# Deploy to Render at tagpropugs.com

We host on [Render](https://render.com). Railway paused new signups after its October 2026 security
incident, and Render offers the same features: Docker deploys from GitHub, managed Postgres,
always-on services, WebSockets, background workers, and custom domains with free HTTPS.

What's already in the repo:

- `Dockerfile`: builds the image and starts uvicorn on Render's `$PORT` with one worker.
- `render.yaml`: a Render **Blueprint** that creates the website and its Postgres database in one
  go. It runs `python -m app.seed` before each deploy (this does nothing once a season exists),
  health-checks `/healthz`, keeps one instance, and fills in `DATABASE_URL` and `SESSION_SECRET`
  for you.
- Postgres support: the app accepts Render's `postgresql://` URL as is.

**Cost:** about $13 a month for the site ($7 web service plus $6 Postgres) on the free Hobby
workspace. MexBot adds $7 (see step 7). Render's free tier isn't suitable, because free web
services go to sleep and free databases expire.

Everything below is done in your own accounts. Secrets only ever go into Render's
**Environment** settings, never into the repo.

## 1. Get the domain

Buy `tagpropugs.com` at a registrar; it was unregistered when we checked on October 28, 2026.
**Cloudflare Registrar** is the easiest choice, because Cloudflare DNS can point the bare domain
(no `www`) at Render. Porkbun and Namecheap also work.

## 2. Create the services from the Blueprint

1. Sign up at https://render.com with your GitHub account and add a payment method.
2. Click **New → Blueprint**, connect GitHub if asked, and pick `notevenmex/tagpro-matchmaking`.
   Render reads `render.yaml` and lists what it will create: the `tagpropugs` web service and the
   `tagpropugs-db` database.
3. Render asks for the values marked secret. Fill them in:
   - `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET`: from
     https://discord.com/developers/applications, your app → **OAuth2**.
   - `ADMIN_DISCORD_IDS`: your Discord user ID, comma-separated if there are more admins.
   - `DISCORD_BOT_TOKEN`: your app → **Bot → Reset Token**. Leave it blank if the bot isn't set up yet.
   - `DISCORD_GUILD_ID`: your Discord server's ID.
   - `VOICE_CATEGORY_ID`, `VOICE_LOBBY_CHANNEL_ID`, `VOICE_STAFF_ROLE_ID`: printed by
     `python -m app.discord_setup` (see step 5). Leave them blank for now if the server isn't set up yet.
4. Click **Apply**. Render creates the database, builds the image, and deploys it. Under the web
   service's **Logs** you should see `Seeded season 4, ...` from the pre-deploy step, then uvicorn
   starting.

To check it before the domain is set up, open the `https://tagpropugs.onrender.com` address shown
at the top of the service. The exact name may differ. Pages load there, but Discord login only
works on the real domain.

To change settings later, use the service's **Environment** tab. Matchmaking settings from
`.env.example` (`QUEUE_SIZE`, `SERVERS`, `MAX_ACCOUNTS`, ...) can be added there if you want
something other than the defaults.

## 3. Point tagpropugs.com at Render

1. In the web service, open **Settings → Custom Domains → Add Custom Domain** and enter
   `tagpropugs.com`. Render adds `www.tagpropugs.com` too and redirects it to the bare domain.
2. Render shows the DNS records to create, for the bare domain and for `www`. Add exactly those at
   your DNS provider, and delete any `AAAA` records for the domain.
   - **Cloudflare:** set the records to **DNS only** (grey cloud) so Render can issue the HTTPS
     certificate.
   - **Other registrars:** if they can't point the bare domain the way Render asks, use the A record
     option Render offers, or move DNS to Cloudflare (free) by changing the nameservers.
3. Click **Verify** in Render. The certificate is issued automatically, usually within minutes.
   It can take a few hours while DNS spreads.

## 4. Update Discord login

In https://discord.com/developers/applications → your app → **OAuth2 → Redirects**, add:

```
https://tagpropugs.com/auth/callback
```

Keep `http://localhost:8000/auth/callback` there for local testing. The Blueprint already sets
`DISCORD_REDIRECT_URI` to that exact value. They must match character for character, with https and
no trailing slash.

Then open https://tagpropugs.com, log in with Discord, register, and check that you see the admin
tabs.

## 5. Turn on the Discord bot features

There is no separate service for the site's own bot. Captain DMs, auto TagPro groups and team voice
channels run as background tasks inside the web service, using only Discord's REST API. Add these
in the web service's **Environment** tab, then save, which redeploys:

1. Invite the bot to the community Discord server with the link from `docs/discord-server.md`,
   then run `app.discord_setup` as that doc describes and paste the IDs it prints here.
2. **Auto TagPro groups:** `AUTO_GROUP=1`, `INSTALL_BOT=1` (makes the build install Playwright and
   Chromium; Render passes environment variables to the Docker build), and the `TAGPRO_*` settings
   from `.env.example`. Chromium is memory-hungry: if the service restarts with out-of-memory
   errors, change the instance type to the 2 GB plan ($25 a month).
3. **Team voice channels:** the Blueprint already sets `VOICE_CHANNELS=1`. Fill in `VOICE_CATEGORY_ID`,
   `VOICE_LOBBY_CHANNEL_ID` and `VOICE_STAFF_ROLE_ID` if you left them blank. The optional settings
   and their defaults are `VOICE_GRACE_SECONDS=120`, `VOICE_MAX_HOURS=3`, `VOICE_DMS=1` and
   `VOICE_PRIVATE=0`.

## 6. Results from tagpro.eu

Once the tagpro.eu results feature is merged, it runs inside the web service too and calls
https://tagpro.eu. Render allows outbound traffic by default, so nothing needs setting up.

## 7. Host MexBot (the music bot)

MexBot is a separate, always-on music bot. It logs in as the same Discord bot as the site
(same token), but it doesn't talk to the site or the database. It lives in `mexbot/` with its own Dockerfile, and runs as a Render **Background
Worker** ($7 a month). Its full steps are in `mexbot/README.md`. In short:

1. **New → Background Worker**, pick this repo, and set:
   - **Language:** Docker
   - **Root Directory:** `mexbot`
   - **Dockerfile Path:** `./Dockerfile`
   - **Instance type:** the $7 one
   - **Name:** `mexbot`
2. Environment: `DISCORD_TOKEN` (the same value as the site's `DISCORD_BOT_TOKEN`), plus optional `MEXBOT_GUILD_ID` (slash commands
   show up instantly in that server) and `YOUTUBE_PROXY` (only if YouTube blocks Render's servers).
3. Keep it at one instance, so there's only one copy of the bot online.

If the MexBot block has been added to `render.yaml`, step 1 happens automatically: open the
Blueprint and click **Sync** instead.

## Things to know

- **Keep one instance.** The queue lock and the live-update WebSocket list are in memory, so a
  second instance or a second uvicorn worker would split players across two queues. Render
  supports WebSockets with no extra setup.
- **Deploys:** every push to `main` redeploys the site. Changes only under `mexbot/`, `docs/` or
  `tests/` don't. Render waits for `/healthz` before switching traffic, so a broken build or bad
  database URL won't take the site down. A deploy restarts the process, which briefly drops live
  connections; browsers reconnect on their own.
- **Database changes:** tables are created on startup, but new columns on existing tables are not
  added automatically. Until we add migrations, a model change that alters an existing table needs
  a manual `ALTER TABLE`. Connect with the database's **Connect → PSQL Command**.
- **Maps and rules:** a fresh database is seeded with the 12 MLTP majors, and admins can edit the
  Rules page and the map pool on the site. A database seeded before that change keeps the 2017
  map list until an admin presses **Use MLTP majors**.
- **If you deployed before alt accounts were added:** `players.discord_id` is no longer unique, and
  startup won't drop the old constraint. Run this once in `psql`:
  `ALTER TABLE players DROP CONSTRAINT players_discord_id_key; CREATE INDEX IF NOT EXISTS ix_players_discord_id ON players (discord_id);`
- **Backups:** paid Render Postgres plans include recovery options. Check the database's
  **Recovery** tab, and take an occasional `pg_dump` with the external connection URL for your own copy.
- **Secrets:** never put real values in `.env.example` or any committed file. If a secret leaks,
  rotate it in Discord and update it in Render's **Environment** tab.
