# Deploy to Railway at tagpropugs.com

What's already in the repo:

- `Dockerfile`: builds the image and starts uvicorn on Railway's `$PORT` with one worker.
- `railway.json`: tells Railway to use the Dockerfile, run `python -m app.seed` before each deploy
  (it does nothing once a season exists), health-check `/healthz`, and keep one replica.
- Postgres support: paste Railway's `DATABASE_URL` as is. The app switches it to the psycopg driver.

Everything below is done in your own accounts. Nothing here needs a secret committed to the repo:
secrets only ever go into Railway's **Variables** tab.

## 1. Get the domain

Search for `tagpropugs.com` at a registrar. If it's free, buy it there. **Cloudflare Registrar**
is the easiest choice, because Cloudflare DNS can point the bare domain (no `www`) at Railway.
Porkbun and Namecheap also work. If you already own the domain, skip this step.

## 2. Create the Railway project

1. Sign up at https://railway.com with your GitHub account. Pick the **Hobby** plan; the free trial
   doesn't keep a site up long term. Check https://railway.com/pricing for the current price.
2. Click **New Project → Deploy from GitHub repo** and pick `notevenmex/tagpro-matchmaking`. If it
   isn't listed, click **Configure GitHub App** and give Railway access to that repo.
3. The first deploy will fail because nothing is configured yet. That's expected.
4. In the same project, click **Create → Database → Add PostgreSQL**.

## 3. Set the variables

Open the web service (the one named after the repo), go to **Variables**, and use **Raw Editor** to
paste this, filling in the blanks:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
SESSION_SECRET=
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
DISCORD_REDIRECT_URI=https://tagpropugs.com/auth/callback
ADMIN_DISCORD_IDS=
HTTPS_ONLY=1
DEV_LOGIN=0
```

- `DATABASE_URL`: keep the `${{Postgres.DATABASE_URL}}` text exactly. Railway fills it in from the
  database. If you renamed the database service, use its name instead of `Postgres`.
- `SESSION_SECRET`: a long random string. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`. If it changes, everyone is logged out.
- `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`: from https://discord.com/developers/applications,
  your app → **OAuth2**.
- `ADMIN_DISCORD_IDS`: your Discord user ID, comma-separated if there are more admins.
- Add any matchmaking settings from `.env.example` (`QUEUE_SIZE`, `SERVERS`, ...) if you want
  something other than the defaults.

Click **Deploy** (or **Apply changes**). Under **Deployments → View logs** you should see
`Seeded season 4, ...` from the pre-deploy step, then uvicorn starting.

To check it before the domain is set up: **Settings → Networking → Generate Domain** gives you a
`*.up.railway.app` address. Pages load there, but Discord login only works on the real domain.

## 4. Point tagpropugs.com at Railway

1. In the web service, go to **Settings → Networking → Custom Domain**, enter `tagpropugs.com`, and
   keep the port Railway suggests (it's the `PORT` the app listens on).
2. Railway shows two DNS records: a **CNAME** for `tagpropugs.com` pointing at something like
   `xxxx.up.railway.app`, and a **TXT** record named `_railway-verify` for ownership.
3. Add both records at your DNS provider, with the exact values Railway shows.
   - **Cloudflare:** a CNAME on the bare domain (`@`) is fine because Cloudflare flattens it. Set
     the CNAME to **DNS only** (grey cloud) so Railway can issue the HTTPS certificate. You can
     turn the orange cloud on later if you want; then set SSL/TLS mode to **Full**.
   - **Other registrars:** use an **ALIAS** or **ANAME** record for `@` if they offer it. If they
     only allow CNAME on subdomains, move DNS to Cloudflare (free) by changing the nameservers.
4. Optional: add `www.tagpropugs.com` as a second custom domain the same way (a plain CNAME for
   `www`).
5. Wait for Railway to show the domain as verified with a certificate. This is usually minutes,
   occasionally up to a few hours while DNS spreads.

## 5. Update Discord login

In https://discord.com/developers/applications → your app → **OAuth2 → Redirects**, add:

```
https://tagpropugs.com/auth/callback
```

Keep `http://localhost:8000/auth/callback` there for local testing. The value must match
`DISCORD_REDIRECT_URI` on Railway character for character (https, no trailing slash). If you also
serve `www`, log in through the bare domain, or add the `www` callback and pick one for the variable.

Then open https://tagpropugs.com, log in with Discord, register, and check that you see the admin
Report tab.

## 6. Turn on the Discord bot (once those PRs are merged)

There is no separate bot service. The bot (captain DMs, auto TagPro groups, team voice channels)
runs as background tasks inside the web service and only uses Discord's REST API, so it shares the
one replica. To turn it on, add variables to the web service:

1. `DISCORD_BOT_TOKEN` (Developer Portal → your app → **Bot → Reset Token**) and
   `SITE_URL=https://tagpropugs.com`. Invite the bot to the community Discord server, since bots
   can only DM people they share a server with.
2. **Auto TagPro groups:** `AUTO_GROUP=1`, `INSTALL_BOT=1` (makes the build install Playwright and
   Chromium, about 400 MB more, which is why it's off by default), and the `TAGPRO_*` settings
   from `.env.example`.
3. **Team voice channels:** `DISCORD_GUILD_ID`, `VOICE_CHANNELS=1`, `VOICE_CATEGORY_ID`, and
   optionally `VOICE_LOBBY_CHANNEL_ID`, `VOICE_STAFF_ROLE_ID`, `VOICE_GRACE_SECONDS`,
   `VOICE_MAX_HOURS`, `VOICE_DMS`. Voice needs nothing extra in the image. Server setup and what
   each one does: `docs/discord-server.md`.
4. Redeploy.

## Things to know

- **Keep one replica.** The queue lock and the live-update WebSocket list are in memory, so a
  second replica or a second uvicorn worker would split players across two queues. Railway
  supports WebSockets with no extra setup.
- **Deploys:** every push to `main` redeploys. Railway waits for `/healthz` before switching
  traffic, so a broken build or bad database URL won't take the site down.
- **Database changes:** tables are created on startup, but new columns on existing tables are not
  added automatically. Until we add migrations, a model change that alters an existing table
  needs a manual `ALTER TABLE` (Postgres service → **Data** or **Query**) or a migration step.
- **Backups:** on the Postgres service, open **Backups** and turn on scheduled backups if your plan offers them.
  Otherwise, run `pg_dump` against the database's public URL now and then.
- **Secrets:** never put real values in `.env.example` or any committed file. If a secret leaks,
  rotate it in Discord and update the Railway variable.
