# Ranked PUGs remake: project handoff

Archive of the planning conversation from October 28, 2026, condensed so it can be picked up in Cowork. The code lives in `ranked-pugs.zip` (unzip it into the Cowork working folder; this file is also included inside it as `HANDOFF.md`).

## Goal

Rebuild the defunct TagPro Ranked PUGs site (originally at `lejdesigns.com/rankedPUGs/`) in the same visual style, but with a FACEIT-style captains system instead of auto-balanced teams.

## What the original site was

Found on the Wayback Machine. The path is case-sensitive: `rankedPUGs`, not `rankedpugs`.

| Page | Capture used | Capture count |
|---|---|---|
| `/rankedPUGs/` (home) | July 1, 2017 | 127 captures, Oct 2014 – Aug 2025 |
| `/rankedPUGs/leaderboard.php` (Top 100) | June 11, 2017 | 68 captures |
| `/rankedPUGs/maps.php` | June 26, 2017 | 47 captures |

Layout: dark charcoal page, large two-tone "RANKED PUGS" wordmark with a slanted yellow "Season 4" tag, a centered nav (Home, Top 100, Maps, Rules, Search), and a green announcement banner with a yellow highlighted sentence. The home page had Login (TagPro name, password) and Register (TagPro name, Reddit name, password, confirm, position, server) forms, with a note that TP League accounts worked too.

Season 4 banner text: welcome to Season 4, points aren't doubled, no one will reach 50; map votes reset; maps can now be rated negatively.

Original matchmaking: teams were made automatically from ranked ratings.

### Top 100 (June 11, 2017). Visible ranks only

Ranks 11–20, 31–40, 51–60, 71–80 and 91–100 were below the fold and not captured. #1 okthen and #2 okthan showed a slash icon instead of a rating (likely placeholder or admin accounts) and were left out. Dot color is assumed to mean position (orange = offense, red = defense); Hulzy appears twice with different colors, which supports that.

| Rank | Player | Rating | Pos | Tags |
|---|---|---|---|---|
| 3 | Warriors | 43 | O | |
| 4 | Neon | 42 | O | S3 50 |
| 5 | swerve | 41 | D | OG, S3 ★, S3 50, S3 Top 10 |
| 6 | Mr Hat | 41 | O | S3 50 |
| 7 | NotEvenMex | 36 | D | |
| 8 | Cosine180_ | 35 | O | |
| 9 | BallGod | 35 | O | S1 50, S3 50 |
| 10 | Crippy | 33 | O | OG, S1 50, S1 Top 10, S3 50, S3 Top 10 |
| 21 | ProTagonist_ | 27 | O | OG, S1 50, S3 50, S3 Top 10 |
| 22 | Nebuchanezar | 26 | O | OG |
| 23 | dopesayo | 26 | O | |
| 24 | ThadCastleee | 25 | O | S3 50 |
| 25 | bdll | 24 | D | |
| 26 | Sleepy | 24 | O | |
| 27 | Dalek23 | 23 | O | OG, S3 50 |
| 28 | Hulzy | 23 | O | OG |
| 29 | swerved | 22 | O | S3 50, S3 Top 10 |
| 30 | GOOBR | 22 | O | |
| 41 | slam | 19 | O | |
| 42 | okthen-mute | 19 | O | |
| 43 | Stann | 18 | O | OG |
| 44 | Pinkman | 18 | O | |
| 45 | BallDon'tLie | 18 | O | OG |
| 46 | noMistakes | 17 | O | |
| 47 | Juke of Yore | 17 | O | |
| 48 | Hulzy | 17 | D | |
| 49 | Pulpo | 16 | O | |
| 50 | (crude name, omitted) | 16 | D | |
| 61 | Eoshy | 14 | O | |
| 62 | dodsfall | 14 | O | OG |
| 63 | quibble | 14 | O | |
| 64 | 2P | 14 | O | |
| 65 | dowen | 13 | O | |
| 66 | mahatma | 13 | O | |
| 67 | Cooostanza | 13 | O | |
| 68 | TOXIC | 12 | O | |
| 69 | eee | 12 | O | OG, S3 50, S3 Top 10 |
| 70 | PiAcc | 12 | D | |
| 81 | DogStrangler | 11 | O | |
| 82 | Megan | 11 | D | |
| 83 | anti-re | 11 | O | |
| 84 | Spud | 10 | O | |
| 85 | robert | 10 | O | |
| 86 | smurf l0rd | 10 | O | |
| 87 | DudeMcGuy | 10 | O | OG |
| 88 | Balwas | 10 | O | |
| 89 | ayy_lmao2 | 10 | O | |
| 90 | VinsantyNZ | 9 | O | |

Tag meanings are guesses: OG = early/original player; S1 50 / S3 50 = reached 50 that season; Top 10 = finished top 10; ★ (crown) = season winner.

### Maps (June 26, 2017)

Votes run from −5 to 10. Vote lists were truncated in the screenshot, so the totals below don't fully reproduce the archived chances. The 10 maps shown add up to about 65%; another row of maps (one with a broken image) was below the fold.

| Map | Archived chance | Visible votes |
|---|---|---|
| Pilot | 13.89% | LEBRON\*JAMES 10, Spud 10, i'm high 10, Dalek23 7, MrJoehobo 7, Beast Mode −5, Gman8181 10, swerve 10 |
| Emerald | 10.3% | LEBRON\*JAMES 10, i'm high 10, Spud 10, Dalek23 9, MrJoehobo −4, Beast Mode −5, Gman8181 10, swerve 10 |
| Scorpio | 10.28% | LEBRON\*JAMES 10, i'm high 10, Spud 10, Dalek23 10, MrJoehobo 5, Beast Mode −5, Gman8181 10, swerve 10 |
| Rush | 8.61% | LEBRON\*JAMES 10, i'm high 10, Spud 10, Dalek23 10, MrJoehobo 6, Beast Mode −5, Gman8181 10, swerve 10 |
| Constriction | 8% | LEBRON\*JAMES 10, Spud 1, Dalek23 7, MrJoehobo 5, Beast Mode −5, Gman8181 10, swerve −5, DT 7 |
| Iron | 3.88% | i'm high −5, Spud −5, LEBRON\*JAMES −5, Dalek23 −5, MrJoehobo 10, Beast Mode −5, Gman8181 −5, swerve 10 |
| Mapache Chief | 3.82% | LEBRON\*JAMES 10, i'm high 10, Spud 10, Beast Mode 10, Dalek23 −5, MrJoehobo 10, Gman8181 −5, swerve −5 |
| Monarch | 3.64% | LEBRON\*JAMES −5, Spud −5, Dalek23 7, MrJoehobo 6, Beast Mode −5, Gman8181 −5, swerve 10, DT 8 |
| Boombox | 2.06% | i'm high 10, Spud 1, LEBRON\*JAMES −5, Dalek23 −5, MrJoehobo 10, Beast Mode −5, Gman8181 −5, swerve 10 |
| Bombing Run | 0.9% | i'm high −5, Spud 10, LEBRON\*JAMES −5, Dalek23 −5, MrJoehobo 10, Beast Mode −5, Gman8181 −5, TheEloReaper −5 |

## What's been built

**1. Front-end prototype** (static, no backend): published at https://claude.ai/artifact/KgDo58ypdCTxcK7cxpirBt. Same layout as the original with an original wordmark; generated map thumbnails, not the real maps. Superseded by the version inside the project.

**2. Full project, `ranked-pugs.zip`** (started fresh, not based on Road to the Super):

- Stack: Python, FastAPI, SQLAlchemy 2, SQLite locally / Postgres in production. The front-end is a single `static/index.html` served by the same app.
- `app/db.py`: models for Season, Player, Rating (per player, per season, per position), Map, MapVote, Match, MatchPlayer.
- `app/auth.py`: Discord OAuth2 login (identify scope only), session cookies, admin list from `ADMIN_DISCORD_IDS`, plus a `DEV_LOGIN=1` test sign-in for local use only.
- `app/rating.py`: team Elo on team averages, K = 32, start 1500, 10 games to appear on the Top 100. Displayed "points" = (Elo − 1400) / 10, so ratings look like the original scale.
- `app/main.py`: API for season, me, register, leaderboard, player search and detail, maps with chances, voting, admin match reporting.
- `app/seed.py`: loads Season 4 with the archived players and map votes above. Archived players have no Discord linked; an admin sets `discord_id` to hand one to its owner.
- Map chance = each map's vote total (floored at 0) ÷ total across all maps.
- `tests/test_api.py`: 2 tests covering public pages, registration, name conflicts, voting rules, admin-only reporting, and rating changes. Both pass.
- README covers local setup, Discord app setup, and deployment.

## Decisions so far

- Start the backend fresh rather than reusing Road to the Super.
- Login through Discord; players register a TagPro name, optional Reddit name, position and server.
- Ratings are per position, so a player can rank separately on O and D.
- Replace the original auto-balanced teams with a captains system modeled on FACEIT.

## Proposed captains flow (not built yet)

1. **Queue** fills to 8.
2. **Ready check**: about 30 seconds to accept; anyone who doesn't gets a short queue ban and the rest return to queue.
3. **Captains**: the two highest-rated players by default, with random or volunteer as options.
4. **Draft**: captains pick the other 6 in 1-2-2-1 order (A, B, B, A, A, B). The pool shows each player's main position and rating so captains can build 2 O / 2 D.
5. **Map**: draw 3–5 maps weighted by the community vote chances, then captains alternate bans down to one.
6. **Server**: quick vote by all 8.
7. **Group**: captain A creates the TagPro group and pastes the link into the match page.
8. **Reporting**: both captains enter the final score. Matching reports confirm automatically and update ratings; disagreements go to an admin.

This needs live updates (WebSockets) for the queue, ready check and draft screens.

## Open questions

- Where the queue and draft happen: on the website with a live draft page, in a Discord bot, or both (website draft, Discord "match found" pings). This was the last unanswered question.
- ~~Whether tagpro.eu or another stats source is still available for automatic result collection.~~ tagpro.eu is used (docs/tagpro-eu.md).
- Whether ratings should consider anything beyond win/loss (cap differential, individual stats).
- Real rules text for the Rules page (currently placeholders).
- Hosting choice (Railway, Render, Fly.io or a VPS) and a domain.

## Suggested next steps

1. Run the project locally with `DEV_LOGIN=1` and the seed to check everything works.
2. Decide where the queue and draft live.
3. Build the queue, ready check, draft, and map veto with WebSockets, and switch match reporting from admin-only to captain reports with admin dispute handling.
4. Set up the Discord application and deploy.
