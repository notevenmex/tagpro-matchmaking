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

Setting names and values below come from the live group page (client 3.2.1, captured 2026-10-28).
Every setting is sent as a string; checkboxes are `"true"`/`"false"`.

| Setting | Value | Why |
|---|---|---|
| `mode` | `classic` | capture the flag |
| `time` | `8` | PUG length in minutes (TagPro's default is 6) |
| `overtime` | `true` | ties go to overtime, with `overtimeRespawnIncrement` `0` |
| `caps` | `0` | no cap limit |
| `mercyRule` | `6` | the game ends when a team leads by 6 (default 3) |
| `kissingFCs` | `false` | no-kiss: flag carriers who touch don't pop each other |
| `potatoTime` | `0` | no potato |
| `noScript` | `true` | |
| `respawnWarnings` | `false` | |
| `redTeamName`, `blueTeamName` | `PUG<match id>` | so tagpro.eu can find the game (see [tagpro-eu.md](tagpro-eu.md)) |
| `regions` | the voted region | one of `US East`, `US Central`, `US West`, `Europe`, `Oceanic` |
| `map` | the drafted map | matched against the map dropdown's options |

Everything else (physics, respawn timers, powerups) stays at TagPro's defaults. The group is
private and not listed publicly, and is named `PUG #<match id>`.

The bot checks the settings the server echoes back. If `kissingFCs` comes back as anything but
`false`, that attempt fails, so a group never goes out with kissing on. After 3 failures, the
captains get a DM asking them to make the group themselves.

Overrides: `TAGPRO_GROUP_NAME`, `TAGPRO_TEAM_NAME`, `TAGPRO_GROUP_SETTINGS` (a JSON object merged
over the table above), and `TAGPRO_GROUP_PRESET` (picks a preset on the create form by its label).

## Servers

A normal group has no server dropdown. Instead it has region checkboxes, all on by default, sent as
`("setting", {name: "regions", value: "US East,US Central"})`. The site's server vote offers the
same five region names (`SERVERS`), and the bot sets `regions` to the winning one alone.

## What could block automation

- **Unverified against the live site.** The container this was built in can't reach
  tagpro.koalabeast.com. The flow is tested against a mock of the page (form labels, `#create-group-btn`,
  `select[name=map]`, the socket events above). The setting names were since checked against the
  live page, but the full flow hasn't run there yet, so expect small fixes on the first real run.
  Errors are logged per lobby and stored on `group_jobs.error`.
- **Captcha / bot protection.** If the create page shows a captcha, the bot reports it and falls
  back to the manual flow. It does not try to solve captchas.
- **Leadership hand-off.** The bot makes the first person who joins the leader with the
  `leader` socket event (configurable via `TAGPRO_LEADER_EVENT`). The live page has this event; the
  log line says whether the hand-off worked. If it doesn't, the bot still leaves after a captain joins, and
  leadership should pass to the remaining member.
- **Guest vs account.** Guests can create groups, so no TagPro account is needed. To create them
  from a named account, set `TAGPRO_SESSION_COOKIE` (copied from a logged-in browser). Keep it out
  of git.
- **Terms of service.** I found no TagPro rule about automating group creation, but this is a bot
  using the site's UI. Worth a quick OK from the TagPro devs on their Discord before going live,
  and keep volume low (one group per match, one browser at a time).
- **Discord DMs.** A bot can only DM users who share a server with it and allow DMs from server
  members. Failed DMs are logged; the link is always on the match page too.
