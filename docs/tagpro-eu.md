# Match results and stats from tagpro.eu

How `app/tagpro_eu.py` finds each match's game on [tagpro.eu](https://tagpro.eu) (TagPro Analytics),
sets the final score from it and saves player stats. Research notes from October 2026.

## How tagpro.eu gets games

tagpro.eu has no upload of its own. Its userscript (`https://tagpro.eu/userscript.user.js`) records a
game in the player's browser and posts it to `/submit/` when the game ends. **A game only exists on
tagpro.eu if at least one person in it (player or spectator) runs the userscript.** Most competitive
players do, but it isn't guaranteed, which is why captain reporting stays as the fallback.

## Finding the game

There is no search API. The site has HTML search pages and a JSON download per match:

| What | URL |
| --- | --- |
| Games by team name (official servers) | `/?search=team-official&name=<team>` |
| Games of a registered player | `/?search=player-verified&name=<name>` |
| One game's raw data (JSON) | `/data/?match=<id>` |
| One game's page | `/?match=<id>` |

The group name isn't recorded by tagpro.eu, but **team names are** (its "Teams" section and team
search), so the auto group bot names both teams `PUG<match id>` (`TAGPRO_TEAM_NAME`) and the group
`PUG #<match id>` (`TAGPRO_GROUP_NAME`). When the red captain makes the group by hand, the match page
asks them to use the same team name.

Every 90 seconds, starting 9 minutes after the group link goes up, the worker:

1. reads the team search for `PUG<id>`, plus the player search for both captains and two more players
   (covers groups without the team names), and any tagpro.eu link a captain or admin pasted on the
   match page;
2. downloads each new match id's JSON (ids it already ruled out are remembered);
3. accepts a game that **finished**, **started after the group link went up**, and has **at least 6
   of our 8 players** (`TAGPRO_EU_MIN_ROSTER`, names compared case-insensitively). Which TagPro team
   is our red comes from where our players started. A different map is logged but allowed.

If the teams played more than once, the first finished game counts. The tagpro.eu score then
finishes the match exactly like two matching captain reports (`reporting.finish`, Elo included),
recorded as `reported_by = "tagpro.eu"`.

## Fallback to captains

For the first 25 minutes after the group link (`TAGPRO_EU_WAIT_MINUTES`), captains can't report and
can't queue; the match page says the score is coming from tagpro.eu. After that the captain report
form opens as before and both captains get a Discord DM asking them to report (if the bot token is
set). The search keeps running in the background for 3 hours (`TAGPRO_EU_GIVE_UP_HOURS`) while the
match is unsettled, so a late tagpro.eu record still settles a dispute. Admins can settle any match
at any time as before. `TAGPRO_EU=0` turns all of this off.

## Player stats

Per-player stats aren't in the JSON as numbers. Each player has an `events` blob: a bit stream of
everything they did (grabs, captures, drops, pops, returns, tags, powerups, prevent/button/block,
team changes), in the format documented on `https://tagpro.eu/?science`. `player_stats()` replays it
and keeps caps, returns, tags, grabs, drops, pops, hold, prevent and time played. They're saved per
match in `player_stats`, for the 8 players whose names match.

The draft and match pop-ups show each player's **season totals** (caps · returns · hold time), with
per-game averages and the number of games on hover. Totals are only from matches whose result came
from tagpro.eu; captain-reported matches have no stats.

## Caveats

- **Not tested against the live site.** The container this was built in can't reach tagpro.eu, so the
  search pages and JSON were handled from documentation, the match pages and the community Python
  parser (`tagpro-eu` on PyPI). Tests run against a fake tagpro.eu. Check the logs after the first
  real matches.
- **robots.txt.** tagpro.eu's `robots.txt` disallows crawlers on `/data/`. This is a handful of
  targeted requests per match (not crawling), with an identifying User-Agent, but it's worth asking
  the tagpro.eu owner (Ronding) for an OK before launch.
- **Team names.** Setting team names goes through the same socket settings as the rest of the
  competitive setup (`redTeamName`/`blueTeamName`), which tagpro-comp-stats uses. If TagPro limits
  their length, keep `TAGPRO_TEAM_NAME` short.
- **Names.** Stats are matched by in-game name. A player whose site name differs from their TagPro
  name won't get stats, and doesn't count toward the 6.
