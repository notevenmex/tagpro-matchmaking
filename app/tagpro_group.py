"""Create a private TagPro group with competitive settings, the drafted map and the voted server.

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
# How long the bot keeps the group open waiting for a captain, then hands over leadership and leaves.
LINGER_SECONDS = int(os.getenv("TAGPRO_BOT_LINGER_SECONDS", "600"))
LEADER_EVENT = os.getenv("TAGPRO_LEADER_EVENT", "leader")  # socket event that makes someone else leader
CREATE_TIMEOUT_MS = int(os.getenv("TAGPRO_CREATE_TIMEOUT_MS", "20000"))
CHROMIUM_PATH = os.getenv("TAGPRO_CHROMIUM_PATH") or None  # only if not using `playwright install chromium`

# Standard competitive group settings (the league defaults that tagpro-comp-stats applies), with PUG
# game length and mercy rule.
COMPETITIVE_SETTINGS: dict[str, str] = {
    "mode": "classic",
    "redTeamName": "Red",
    "blueTeamName": "Blue",
    "redTeamScore": "0",
    "blueTeamScore": "0",
    "time": "8",                       # minutes (PUG length)
    "caps": "0",                       # no cap limit
    "mercyRule": "6",                  # game ends when a team leads by 6 caps
    "overtime": "true",
    "overtimeRespawnIncrement": "0",
    "overtimeJukeJuice": "false",
    "accel": "1",
    "topspeed": "1",
    "bounce": "1",
    "playerRespawnTime": "3000",
    "speedPadRespawnTime": "10000",
    "dynamiteRespawnTime": "30000",
    "buffRespawnTime": "60000",
    "buffDelay": "true",
    "potatoTime": "0",
    "noScript": "true",
    "respawnWarnings": "false",
    "pupIndicators": "true",
    "rollingBombBehavior": "default",
    "powerupJukeJuice": "true",
    "powerupTagPro": "true",
    "powerupRollingBomb": "true",
    "combinejjrb": "false",
    "gravity": "false",
    "jumpLimit": "2",
    "speedLimit": "true",
    "eggballLosingTeamStarts": "true",
}


# No-kiss lives under the group page's experimental settings toggle as "Kissing Flag Carriers". The bot
# opens that toggle and switches it off through the page. If its socket name is known, setting
# TAGPRO_NOKISS_SETTING=name=value sends it directly instead. TAGPRO_REQUIRE_NOKISS=0 lets a group
# through even when the option can't be found (by default that counts as a failed attempt).
REQUIRE_NOKISS = os.getenv("TAGPRO_REQUIRE_NOKISS", "1") == "1"


def _no_kiss() -> dict[str, str]:
    """The no-kiss option as a direct socket setting, if configured (see above)."""
    raw = os.getenv("TAGPRO_NOKISS_SETTING", "").strip()
    if not raw:
        return {}
    name, _, value = raw.partition("=")
    return {name.strip(): value.strip() or "true"}


def team_name(lobby_id: int) -> str:
    return TEAM_NAME.format(id=lobby_id)[:30]


def group_settings(lobby_id: int | None = None) -> dict[str, str]:
    """Competitive defaults + no-kiss + the match's team names + any overrides from TAGPRO_GROUP_SETTINGS
    (a JSON object)."""
    out = {**COMPETITIVE_SETTINGS, **_no_kiss()}
    if lobby_id is not None:
        out["redTeamName"] = out["blueTeamName"] = team_name(lobby_id)
    extra = os.getenv("TAGPRO_GROUP_SETTINGS", "").strip()
    if extra:
        out.update({str(k): str(v) for k, v in json.loads(extra).items()})
    return out


# Our server names (the SERVERS vote) -> words to look for in TagPro's server dropdown labels.
DEFAULT_SERVER_KEYWORDS = {
    "us east": ["new jersey", "newark", "new york", "virginia", "ashburn", "toronto", "atlanta", "miami", "east"],
    "us central": ["dallas", "chicago", "central"],
    "us west": ["los angeles", "seattle", "san francisco", "san jose", "west"],
    "europe": ["europe", "london", "frankfurt", "amsterdam", "paris", "eu"],
    "oceania": ["sydney", "oceania", "australia"],
}

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


def pick_server(options: list[tuple[str, str]], wanted: str | None) -> str | None:
    """Pick the dropdown value for a server, using TAGPRO_SERVER_MAP (JSON: our name -> TagPro label)
    first, then the keyword table above, then a plain name match."""
    if not wanted:
        return None
    custom = {k.lower(): v for k, v in json.loads(os.getenv("TAGPRO_SERVER_MAP", "") or "{}").items()}
    want = wanted.strip().lower()
    candidates = []
    if want in custom:
        candidates.append(custom[want].lower())
    candidates += DEFAULT_SERVER_KEYWORDS.get(want, [])
    candidates.append(want)
    for c in candidates:
        for value, label in options:
            if c and (c in (label or "").lower() or c == (value or "").lower()):
                return value
    return None


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

    @property
    def name(self) -> str:
        return GROUP_NAME.format(id=self.lobby_id)[:30]


class GroupError(Exception):
    pass


# Runs inside the page before its scripts: hooks the group socket as soon as it exists, so we see
# every member event (including our own id) and can tell when a captain has joined.
_INIT_SCRIPT = """
window.__rp = {you: null, members: {}, settings: {}};
const __rpIv = setInterval(() => {
  const s = window.tagpro && tagpro.group && tagpro.group.socket;
  if (!s) return;
  clearInterval(__rpIv);
  s.on('you', id => { __rp.you = id; });
  s.on('member', m => { __rp.members[m.id] = m; });
  s.on('removed', m => { delete __rp.members[m.id]; });
  s.on('setting', st => { __rp.settings[st.name] = st.value; });
}, 5);
"""


# Finds the "Kissing Flag Carriers" control, marks it with data-rp-kiss, and says what kind it is.
_FIND_KISS_JS = """
() => {
  const label = [...document.querySelectorAll('label, span, div, td, th, p, b, strong')]
    .filter(el => el.offsetParent !== null && /kissing\\s+flag\\s+carriers/i.test(el.textContent))
    .sort((a, b) => a.textContent.length - b.textContent.length)[0];
  if (!label) return 'missing';
  let el = label.htmlFor ? document.getElementById(label.htmlFor) : null;
  for (let node = label; !el && node && node !== document.body; node = node.parentElement)
    el = node.querySelector('select, input[type=checkbox]');
  if (!el) return 'no control';
  el.setAttribute('data-rp-kiss', '1');
  return el.tagName === 'SELECT' ? 'select' : 'checkbox';
}
"""


class PlaywrightGroupCreator:
    """Creates groups by driving the real TagPro groups page in headless Chromium."""

    def __init__(self):
        self._tasks: set[asyncio.Task] = set()

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
        # Keep the group alive until a captain shows up, without holding up the DMs.
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
        await _emit_settings(page, {"serverSelect": "true", "selectMaps": "true", **settings})
        await page.wait_for_timeout(500)  # let the server/map dropdowns fill in
        if not _no_kiss():
            await self._disable_kissing(page, spec)
        servers = await _options(page, 'select[name="server"]')
        server = pick_server(servers, spec.server)
        if spec.server and not server:
            log.warning("lobby %s: no TagPro server matches %r (options: %s)", spec.lobby_id, spec.server,
                        [label for _, label in servers])
        maps = await _options(page, 'select[name="map"]') or await _options(page, "select.js-socket-setting")
        map_value = pick_map(maps, spec.map_name)
        if spec.map_name and not map_value:
            log.warning("lobby %s: no TagPro map matches %r", spec.lobby_id, spec.map_name)
        await _emit_settings(page, {k: v for k, v in (("server", server), ("map", map_value)) if v})
        await page.wait_for_timeout(500)
        applied = await page.evaluate("() => __rp.settings")
        missing = [k for k in settings if k not in applied]
        if missing:
            log.info("lobby %s: TagPro didn't echo these settings back (may be unknown or unchanged): %s",
                     spec.lobby_id, ", ".join(missing))

    async def _disable_kissing(self, page, spec: GroupSpec):
        """Open "experimental settings" and turn "Kissing Flag Carriers" off, like a leader would."""
        before = dict(await page.evaluate("() => __rp.settings"))
        found = await page.evaluate(_FIND_KISS_JS)
        if found == "missing":  # hidden until the experimental toggle is on
            toggle = page.get_by_label(re.compile("experimental", re.I))
            if await toggle.count():
                if not await toggle.first.is_checked():
                    await toggle.first.check()
            else:
                toggle = page.get_by_text(re.compile("experimental", re.I))
                if await toggle.count():
                    await toggle.first.click()
            await page.wait_for_timeout(500)
            found = await page.evaluate(_FIND_KISS_JS)
        if found == "select":
            control = page.locator("[data-rp-kiss]")
            off = await page.evaluate(
                "() => { const o = [...document.querySelector('[data-rp-kiss]').options]"
                ".find(o => /disabl|\\boff\\b|^no\\b/i.test(o.textContent.trim())); return o ? o.value : null; }")
            if off is None:
                found = "no off option"
            else:
                await control.select_option(off)
        elif found == "checkbox":
            control = page.locator("[data-rp-kiss]")
            if await control.is_checked():
                await control.uncheck()
        if found not in ("select", "checkbox"):
            msg = f"Couldn't turn off Kissing Flag Carriers ({found}); the group page may have changed."
            if REQUIRE_NOKISS:
                raise GroupError(msg)
            log.warning("lobby %s: %s", spec.lobby_id, msg)
            return
        await page.wait_for_timeout(500)
        after = await page.evaluate("() => __rp.settings")
        changed = {k: v for k, v in after.items() if before.get(k) != v}
        log.info("lobby %s: turned off Kissing Flag Carriers (server echoed %s)", spec.lobby_id, changed or "nothing")

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


async def _emit_settings(page, settings: dict[str, str]):
    await page.evaluate(
        "(pairs) => { for (const [name, value] of pairs) tagpro.group.socket.emit('setting', {name, value}); }",
        list(settings.items()))


async def _options(page, selector: str) -> list[tuple[str, str]]:
    return [tuple(x) for x in await page.evaluate(
        "(sel) => { const el = document.querySelector(sel);"
        " return el ? [...el.options].map(o => [o.value, o.textContent.trim()]) : []; }", selector)]
