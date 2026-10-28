# Creating TagPro groups automatically

Research notes for the auto group bot (`app/autogroup.py`, `app/tagpro_group.py`), October 2026.

## How TagPro groups work

There is no public or documented API for groups. Everything goes through the site's own pages:

- **Create:** the form on `https://tagpro.koalabeast.com/groups` has *Name*, *List Group Publicly*,
  *Play Private Games*, *Group Preset* and a *Create Group* button. Submitting it lands you on
  `/groups/<8 lowercase letters>`, and whoever creates the group is its leader.
- **Configure:** the group page opens a socket.io connection (`tagpro.group.socket`). The leader
  changes an option by sending `("setting", {name, value})`, and the server echoes `setting` back
  to everyone. Other events seen in community scripts: `you` (your member id), `member`
  (a member joined or changed, with `leader` and `team`), `removed`, `chat`, `private`,
  `port`, `play`, `swapTeams`.
- **Sources:** [tagpro-comp-stats](https://github.com/Hjalpa/tagpro-comp-stats) (its
  `resetGroupSettings()` sets every competitive option this way and auto-creates groups from
  `/groups/#cg`), and [TagPro GroPro](https://github.com/wilcooo/TagPro-GroPro) (group socket events).

Because the socket.io version, cookies and page are TagPro's, the bot drives the real page in
headless Chromium (Playwright) and sends settings through the page's own socket, the same way those
userscripts do, instead of reimplementing the protocol.

## Settings the bot applies

`COMPETITIVE_SETTINGS` in `app/tagpro_group.py`, taken from the league ("NALTP comp") defaults
with PUG length and mercy: classic mode, 8 minutes, no cap limit, 6-cap mercy rule (the game ends
when a team leads by 6), overtime on (no juke juice, no respawn increment in OT), 100% accel/top speed/bounce, default respawn timers (3s player, 10s boost,
30s bomb, 60s powerup), buff delay on, no potato, `noScript` on, respawn warnings off, powerup
indicators on, all powerups on, gravity off. Plus the drafted map, the voted server, and a private
(not publicly listed) group with private games.

The group is named `PUG #<match id>` and both teams `PUG<match id>`, so the game can be found on
tagpro.eu afterwards (see [tagpro-eu.md](tagpro-eu.md)). Overrides: `TAGPRO_GROUP_NAME`, `TAGPRO_TEAM_NAME`,
`TAGPRO_GROUP_SETTINGS` (JSON), `TAGPRO_GROUP_PRESET` (pick a preset on the create form
by label), `TAGPRO_SERVER_MAP` (our server names to TagPro's labels).

## No-kiss

"No kiss" means two flag carriers who touch don't pop each other. On the group page it's
**Kissing Flag Carriers**, under the experimental settings toggle. After applying the settings
above, the bot turns that toggle on, finds the Kissing Flag Carriers control, and switches it to
off/disabled, so the page sends the setting itself. The log line shows the socket setting the server
echoed back. Once you know that name you can set `TAGPRO_NOKISS_SETTING=name=value` to send it
directly and skip the UI step.

If the control can't be found, that attempt counts as a failure, so a group never goes out with
kissing on. After 3 failures the captains get the "make it yourself" DM. Set `TAGPRO_REQUIRE_NOKISS=0`
to let groups through anyway, with a warning in the log.

## What could block automation

- **Unverified against the live site.** The container this was built in can't reach
  tagpro.koalabeast.com. The flow is tested against a mock of the page (form labels, `#create-group-btn`,
  `select[name=server]`, `select[name=map]`, the socket events above), so expect to adjust selectors
  on the first real run. Errors are logged per lobby and stored on `group_jobs.error`.
- **Captcha / bot protection.** If the create page shows a captcha, the bot reports it and falls
  back to the manual flow. It does not try to solve captchas.
- **Leadership hand-off.** The bot makes the first person who joins the leader with the
  `leader` socket event (configurable via `TAGPRO_LEADER_EVENT`). That event name is a guess; the
  log line says whether it worked. If it doesn't, the bot still leaves after a captain joins, and
  leadership should pass to the remaining member.
- **Guest vs account.** Guests can create groups, so no TagPro account is needed. To create them
  from a named account, set `TAGPRO_SESSION_COOKIE` (copied from a logged-in browser). Keep it out
  of git.
- **Terms of service.** I found no TagPro rule about automating group creation, but this is a bot
  using the site's UI. Worth a quick OK from the TagPro devs on their Discord before going live,
  and keep volume low (one group per match, one browser at a time).
- **Discord DMs.** A bot can only DM users who share a server with it and allow DMs from server
  members. Failed DMs are logged; the link is always on the match page too.
