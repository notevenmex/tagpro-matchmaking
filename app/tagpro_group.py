"""Create a private TagPro group with PUG settings, the drafted map and the voted server region.

TagPro has no public API for groups. The site's own group page (tagpro.koalabeast.com/groups/<id>)
talks to the game server over socket.io, and the leader changes an option by sending
("setting", {"name": ..., "value": ...}). Community scripts such as tagpro-comp-stats set up
competitive groups exactly that way, so we do the same: a headless browser (Playwright) opens the
real page, creates the group, and sends those events through the page's own socket. That keeps us
on whatever socket.io version and session cookies the site uses. See docs/tagpro-groups.md.
"""
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .group_host import GroupHost, Seat

log = logging.getLogger("rankedpugs")

TAGPRO_URL = os.getenv("TAGPRO_URL", "https://tagpro.koalabeast.com").rstrip("/")
GROUP_NAME = os.getenv("TAGPRO_GROUP_NAME", "PUG #{id}")
# Both teams get this name in the group. tagpro.eu lists games by team name, so it's how the site finds
# a match's stats afterwards (see tagpro_eu.py). Keep it short and unique per match.
TEAM_NAME = os.getenv("TAGPRO_TEAM_NAME", "PUG{id}")
# Optional "Group Preset" to pick on the create form, by its visible label (e.g. "Competitive").
GROUP_PRESET = os.getenv("TAGPRO_GROUP_PRESET", "").strip()
# Optional TagPro session cookie, if groups should be owned by a TagPro account instead of a guest.
SESSION_COOKIE = os.getenv("TAGPRO_SESSION_COOKIE", "").strip()
SESSION_COOKIE_NAME = os.getenv("TAGPRO_SESSION_COOKIE_NAME", "tagpro")
# The bot hosts up to this many groups at once (one headless browser each, for the whole match; see
# group_host.py). Past that, it creates the group, waits for a captain, hands over leadership and leaves.
MAX_HOSTED = int(os.getenv("TAGPRO_MAX_HOSTED_GROUPS", "2"))
# In hand-off mode: how long the bot waits for a captain before leaving anyway.
LINGER_SECONDS = int(os.getenv("TAGPRO_BOT_LINGER_SECONDS", "600"))
LEADER_EVENT = os.getenv("TAGPRO_LEADER_EVENT", "leader")  # socket event that makes someone else leader
CREATE_TIMEOUT_MS = int(os.getenv("TAGPRO_CREATE_TIMEOUT_MS", "20000"))
CHROMIUM_PATH = os.getenv("TAGPRO_CHROMIUM_PATH") or None  # only if not using `playwright install chromium`

# Group settings for a PUG. Names and values are the live group page's, captured from tagpro.koalabeast.com
# (client 3.2.1) on 2026-10-28: every setting is a string, checkboxes are "true"/"false". Anything not
# listed stays at TagPro's default (physics, respawn timers, powerups).
PUG_SETTINGS: dict[str, str] = {
    "mode": "classic",
    "redTeamName": "Red",
    "blueTeamName": "Blue",
    "time": "8",                       # minutes, 0-20 (TagPro's default is 6)
    "overtime": "true",
    "overtimeRespawnIncrement": "0",
    "caps": "0",                       # no cap limit
    "mercyRule": "6",                  # 0-10: the game ends when a team leads by 6 caps (default 3)
    "kissingFCs": "false",             # no-kiss: flag carriers who touch don't pop each other
    "potatoTime": "0",
    "noScript": "true",
    "respawnWarnings": "false",
}

# TagPro's server regions, exactly as the group page sends them in the "regions" setting. The site's
# server vote uses these names (SERVERS in play.py), so the winner maps straight onto a region.
REGIONS = ("US East", "US Central", "US West", "Europe", "Oceanic")


def team_name(lobby_id: int) -> str:
    return TEAM_NAME.format(id=lobby_id)[:30]


def group_settings(lobby_id: int | None = None) -> dict[str, str]:
    """PUG defaults + the match's team names + any overrides from TAGPRO_GROUP_SETTINGS (a JSON object)."""
    out = dict(PUG_SETTINGS)
    if lobby_id is not None:
        out["redTeamName"] = out["blueTeamName"] = team_name(lobby_id)
    extra = os.getenv("TAGPRO_GROUP_SETTINGS", "").strip()
    if extra:
        out.update({str(k): str(v) for k, v in json.loads(extra).items()})
    return out


# Map names whose TagPro value doesn't follow from the display name (from tagpro-comp-stats).
MAP_ALIASES = {
    "angrypig": "AngryPig", "bombingrun": "bomber", "centerflag": "centerflag", "commandcenter": "CommandCenter",
    "dangerzone3": "DangerZone", "geokoala": "teamwork", "hurricane": "Hurricane2", "hyperreactor": "HyperReactor",
    "mode7": "Mode7", "snesv2": "snes", "thinkingwithportals": "ThinkingWithPortals", "bigvird": "vee2",
    "blastoff": "blastoff", "containmasters": "ContainMasters", "diamondfaces": "Diamond",
    "eventhorizon": "eventhorizon", "eventhorizon2": "eventhorizon2", "figure8": "map2-2",
    "gloryhole": "RiskAndReward", "grailofspeed": "GrailOfSpeed", "openfieldmasters": "OFM",
    "pokeball": "community1", "pushit": "PushIt", "theholysee": "HolySee", "holysee": "HolySee", "vee": "bird",
    "whirlwind2": "whirlwind", "yiss32": "yiss 3.2", "eggball": "mode/eggball",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pick_map(options: list[tuple[str, str]], wanted: str | None) -> str | None:
    """Pick the dropdown value for a map. options are (value, label) pairs from the group page."""
    if not wanted:
        return None
    w = _norm(wanted)
    alias = MAP_ALIASES.get(w)
    for value, label in options:
        if alias and value == alias:
            return value
        if w and w in (_norm(value), _norm(label)):
            return value
    return None


def region_for(server: str | None) -> str | None:
    """The TagPro region for a voted server name, or None to leave all regions on."""
    if not server:
        return None
    return next((r for r in REGIONS if r.lower() == server.strip().lower()), None)


@dataclass
class Captain:
    team: str            # red | blue
    tagpro_name: str
    discord_id: str | None


@dataclass
class GroupSpec:
    lobby_id: int
    map_name: str | None
    server: str | None
    captains: list[Captain] = field(default_factory=list)
    seats: list[Seat] = field(default_factory=list)  # all drafted players, captains included

    @property
    def name(self) -> str:
        return GROUP_NAME.format(id=self.lobby_id)[:30]


class GroupError(Exception):
    pass


# Runs inside the page before its scripts: hooks the group socket as soon as it exists, so we see
# every member event (including our own id) and can tell when a captain has joined.
_INIT_SCRIPT = """
window.__rp = {you: null, members: {}, settings: {}, chats: []};
const __rpIv = setInterval(() => {
  const s = window.tagpro && tagpro.group && tagpro.group.socket;
  if (!s) return;
  clearInterval(__rpIv);
  s.on('you', id => { __rp.you = id; });
  s.on('member', m => { __rp.members[m.id] = m; });
  s.on('removed', m => { delete __rp.members[m.id]; });
  s.on('setting', st => { __rp.settings[st.name] = st.value; });
  s.on('chat', c => { __rp.chats.push(c); });
}, 5);
"""


class PlaywrightGroupCreator:
    """Creates groups by driving the real TagPro groups page in headless Chromium.

    is_live(lobby_id) says whether the match still needs its group; on_missing(spec, names) is told
    when the teams still aren't full after a while (with a "Red 3/4, Blue 4/4" summary). Both are only used while hosting.
    """

    def __init__(self, is_live: Callable[[int], Awaitable[bool]] | None = None,
                 on_missing: Callable[[GroupSpec, str], Awaitable[None]] | None = None):
        self._tasks: set[asyncio.Task] = set()
        self.is_live, self.on_missing = is_live, on_missing
        self.hosting = 0

    async def create(self, spec: GroupSpec) -> str:
        from playwright.async_api import async_playwright  # optional dependency (requirements-bot.txt)

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(executable_path=CHROMIUM_PATH)
        try:
            ctx = await browser.new_context()
            if SESSION_COOKIE:
                host = re.sub(r"^https?://", "", TAGPRO_URL).split("/")[0]
                await ctx.add_cookies([{"name": SESSION_COOKIE_NAME, "value": SESSION_COOKIE,
                                        "domain": host, "path": "/", "secure": TAGPRO_URL.startswith("https")}])
            await ctx.add_init_script(_INIT_SCRIPT)
            page = await ctx.new_page()
            page.set_default_timeout(CREATE_TIMEOUT_MS)
            await page.goto(f"{TAGPRO_URL}/groups")
            link = await self._create_group(page, spec)
            await self._configure(page, spec)
        except Exception as e:
            await browser.close()
            await pw.stop()
            if isinstance(e, GroupError):
                raise
            raise GroupError(f"{type(e).__name__}: {e}") from e
        # Host the match (or hand over to a captain), without holding up the DMs.
        if self.is_live and spec.seats and self.hosting < MAX_HOSTED:
            task = asyncio.create_task(self._host(pw, browser, page, spec))
        else:
            task = asyncio.create_task(self._hand_over(pw, browser, page, spec))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return link

    async def _create_group(self, page, spec: GroupSpec) -> str:
        name = page.get_by_label("Name", exact=True)
        if await name.count():
            await name.first.fill(spec.name)
        public = page.get_by_label("List Group Publicly")
        if await public.count() and await public.first.is_checked():
            await public.first.uncheck()
        private = page.get_by_label("Play Private Games")
        if await private.count() and not await private.first.is_checked():
            await private.first.check()
        if GROUP_PRESET:
            preset = page.get_by_label("Group Preset")
            if await preset.count():
                await preset.first.select_option(label=GROUP_PRESET)
            else:
                log.warning("TAGPRO_GROUP_PRESET is set but the create form has no Group Preset field")
        button = page.get_by_role("button", name=re.compile("create group", re.I))
        if not await button.count():
            button = page.locator("#create-group-btn")
        if not await button.count():
            raise GroupError("Couldn't find the Create Group button; the TagPro groups page may have changed.")
        await button.first.click()
        try:
            await page.wait_for_url(re.compile(r"/groups/[a-z]{8}/?$"))
        except Exception:
            body = (await page.content()).lower()
            if "captcha" in body or "recaptcha" in body:
                raise GroupError("TagPro asked for a captcha when creating the group.")
            raise GroupError("TagPro didn't open a new group after Create Group.")
        await page.wait_for_function("() => window.tagpro && tagpro.group && tagpro.group.socket "
                                     "&& tagpro.group.socket.connected && __rp.you")
        return page.url.split("#")[0].rstrip("/")

    async def _configure(self, page, spec: GroupSpec):
        settings = group_settings(spec.lobby_id)
        region = region_for(spec.server)
        if spec.server and not region:
            log.warning("lobby %s: %r isn't a TagPro region (%s); leaving all regions on",
                        spec.lobby_id, spec.server, ", ".join(REGIONS))
        if region:
            settings["regions"] = region
        maps = await _options(page, 'select[name="map"]')
        map_value = pick_map(maps, spec.map_name)
        if spec.map_name and not map_value:
            log.warning("lobby %s: no TagPro map matches %r", spec.lobby_id, spec.map_name)
        if map_value:
            settings["map"] = map_value
        await _emit_settings(page, settings)
        await page.wait_for_timeout(1000)
        applied = await page.evaluate("() => __rp.settings")
        wrong = {k: applied[k] for k, v in settings.items() if k in applied and str(applied[k]) != v}
        if "kissingFCs" in wrong:
            raise GroupError(f"TagPro kept kissingFCs={wrong['kissingFCs']}; no-kiss didn't apply.")
        if wrong:
            log.warning("lobby %s: TagPro applied different values: %s", spec.lobby_id, wrong)
        missing = [k for k in settings if k not in applied]
        if missing:
            log.info("lobby %s: TagPro didn't echo these settings back (may be unchanged): %s",
                     spec.lobby_id, ", ".join(missing))

    async def _host(self, pw, browser, page, spec: GroupSpec):
        self.hosting += 1
        try:
            async def live() -> bool:
                return await self.is_live(spec.lobby_id)

            async def missing(counts: str):
                if self.on_missing:
                    await self.on_missing(spec, counts)

            await GroupHost(PageGroupConn(page), spec.lobby_id, spec.seats, live, missing).run()
        except Exception:
            log.exception("lobby %s: error while hosting the TagPro group", spec.lobby_id)
        finally:
            self.hosting -= 1
            try:
                await page.goto(f"{TAGPRO_URL}/groups/leave")
            except Exception:
                pass
            await browser.close()
            await pw.stop()

    async def _hand_over(self, pw, browser, page, spec: GroupSpec):
        try:
            deadline = asyncio.get_running_loop().time() + LINGER_SECONDS
            joined = None
            while asyncio.get_running_loop().time() < deadline:
                joined = await page.evaluate(
                    "() => Object.values(__rp.members).find(m => m.id !== __rp.you) || null")
                if joined:
                    break
                await asyncio.sleep(2)
            if joined:
                await page.evaluate("([ev, id]) => tagpro.group.socket.emit(ev, id)", [LEADER_EVENT, joined["id"]])
                await asyncio.sleep(2)
                now_leader = await page.evaluate("(id) => !!(__rp.members[id] && __rp.members[id].leader)",
                                                 joined["id"])
                log.info("lobby %s: %s joined the group; leadership handed over: %s",
                         spec.lobby_id, joined.get("name"), now_leader)
            else:
                log.warning("lobby %s: nobody joined the TagPro group within %ss", spec.lobby_id, LINGER_SECONDS)
        except Exception:
            log.exception("lobby %s: error while waiting in the TagPro group", spec.lobby_id)
        finally:
            await browser.close()
            await pw.stop()


class PageGroupConn:
    """group_host.GroupConn over the group page's own socket."""

    def __init__(self, page):
        self.page = page

    async def state(self) -> dict:
        return await self.page.evaluate(
            "() => { const c = __rp.chats; __rp.chats = []; return {you: __rp.you, members: __rp.members, chats: c}; }")

    async def emit(self, event: str, payload=None):
        await self.page.evaluate("([ev, p, has]) => has ? tagpro.group.socket.emit(ev, p) : tagpro.group.socket.emit(ev)",
                                 [event, payload, payload is not None])

    async def say(self, text: str):
        await self.emit("chat", text[:120])


async def _emit_settings(page, settings: dict[str, str]):
    await page.evaluate(
        "(pairs) => { for (const [name, value] of pairs) tagpro.group.socket.emit('setting', {name, value}); }",
        list(settings.items()))


async def _options(page, selector: str) -> list[tuple[str, str]]:
    return [tuple(x) for x in await page.evaluate(
        "(sel) => { const el = document.querySelector(sel);"
        " return el ? [...el.options].map(o => [o.value, o.textContent.trim()]) : []; }", selector)]
