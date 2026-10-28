"""Read finished games from tagpro.eu to set match scores and collect player stats.

tagpro.eu (TagPro Analytics) stores games uploaded by its userscript: whenever someone in a game runs
it, the full game is posted when it ends. There is no search API, so the worker reads the site's own
search pages for match ids and then downloads each candidate's JSON (/data/?match=<id>):

- Team search (?search=team-official&name=PUG123). The auto group bot names both teams after the
  match (tagpro_group.TEAM_NAME), so this finds exactly our game.
- Verified player search (?search=player-verified&name=...), for the captains and a few more
  players. It covers groups made by hand without the team names.
- Match links a captain or admin pastes on the match page.

A candidate counts as our game when it finished, started after our group link went up, and at least
MIN_ROSTER of our eight players are in it. The red/blue sides come from where our players started.
The tagpro.eu score then finishes the match like two matching captain reports do (Elo included), and
each player's caps, returns, hold time and so on are saved to player_stats.

Captains can't report while the search is running. After WAIT_MINUTES with nothing found, they're
asked to report the score as before (and DMed, if the Discord bot is set up). The search keeps going
in the background until the match is settled, so a tagpro.eu record that shows up late still settles
a dispute. Turn it off with TAGPRO_EU=0. See docs/tagpro-eu.md.
"""
import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import play
from .db import Lobby, LobbyPlayer, Map, Player, PlayerStat, ResultLookup, SessionLocal
from .tagpro_group import team_name

log = logging.getLogger("rankedpugs")

ENABLED = os.getenv("TAGPRO_EU", "1") == "1"
BASE_URL = os.getenv("TAGPRO_EU_URL", "https://tagpro.eu").rstrip("/")
FIRST_CHECK_SECONDS = int(os.getenv("TAGPRO_EU_FIRST_CHECK_SECONDS", "420"))  # a game is 8 minutes, or less on a mercy
POLL_SECONDS = int(os.getenv("TAGPRO_EU_POLL_SECONDS", "90"))
WAIT_MINUTES = int(os.getenv("TAGPRO_EU_WAIT_MINUTES", "25"))      # then captains report by hand
GIVE_UP_HOURS = float(os.getenv("TAGPRO_EU_GIVE_UP_HOURS", "3"))  # stop searching altogether
MIN_ROSTER = int(os.getenv("TAGPRO_EU_MIN_ROSTER", "6"))           # of our players in the tagpro.eu game
SEARCH_PLAYERS = int(os.getenv("TAGPRO_EU_SEARCH_PLAYERS", "4"))  # player searches per poll, captains first
PER_PAGE = 8  # newest match ids looked at on each search page
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")
UNSETTLED = ("in_progress", "disputed")

FRAMES = 60  # tagpro.eu times are in frames, 60 per second


def match_url(eu_id: int) -> str:
    return f"{BASE_URL}/?match={eu_id}"


def parse_match_id(text: str) -> int | None:
    """A tagpro.eu match id from a pasted link (…/?match=123, …/data/?match=123) or a bare number."""
    m = re.fullmatch(r"\s*(?:https?://(?:www\.)?tagpro\.eu/(?:data/)?\?(?:match|download)=)?(\d{1,10})/?\s*", text)
    return int(m.group(1)) if m else None


# ---------- decoding tagpro.eu match files (format: https://tagpro.eu/?science) ----------
class _Bits:
    """Reads a tagpro.eu blob: a bit stream, most significant bit first, padded with zeros."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def done(self) -> bool:
        return self.pos >> 3 >= len(self.data)

    def bit(self) -> int:
        b = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1 if not self.done() else 0
        self.pos += 1
        return b

    def fixed(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = v << 1 | self.bit()
        return v

    def tally(self) -> int:
        n = 0
        while self.bit():
            n += 1
        return n

    def footer(self) -> int:
        size = self.fixed(2) << 3
        free = (8 - (self.pos & 7)) & 7
        size |= free
        minimum = 0
        while free < size:
            minimum += 1 << free
            free += 8
        return self.fixed(size) + minimum


STAT_KEYS = ("caps", "returns", "tags", "grabs", "drops", "pops", "hold", "prevent", "played")


def player_stats(events_b64: str, team: int, duration: int) -> dict[str, int]:
    """Replay one player's events blob into totals. team is where they started (1 red, 2 blue, 0 none);
    duration is the game length in frames. hold, prevent and played come back in frames."""
    b = _Bits(base64.b64decode(events_b64 or ""))
    s = dict.fromkeys(STAT_KEYS, 0)
    time = flag = powers = 0
    preventing = False
    in_since = 0 if team else -1
    hold_since = prevent_since = -1

    while not b.done():
        new_team = team
        if b.bit():  # joined, left or switched teams
            if not team:
                new_team = 1 + b.bit()
            elif b.bit():
                new_team = 0
            else:
                new_team = 3 - team
        drop_pop = b.bit()
        returns = b.tally()
        tags = b.tally()
        grab = not flag and b.bit()
        captures = b.tally()
        # Whether a grab or capture keeps the flag; the bit is only there when it can't be inferred.
        keep = (not drop_pop and new_team and (new_team == team or not team)
                and (not captures or (not flag and not grab) or b.bit()))
        new_flag = flag
        if grab:
            new_flag = 1 + b.fixed(2) if keep else 5  # 5: a flag held only for this instant
        powerups = b.tally()
        for p in (1, 2, 4, 8):  # juke juice, rolling bomb, tagpro, top speed
            if powers & p:
                if b.bit():
                    powers &= ~p
            elif powerups and b.bit():
                powers |= p
                powerups -= 1
        toggle_prevent = b.bit()
        b.bit(), b.bit()  # button and block toggles: not tracked
        time += 1 + b.footer()

        if not team and new_team:
            team, in_since = new_team, time
        s["returns"] += returns
        s["tags"] += returns + tags
        if grab:
            flag = new_flag
            s["grabs"] += 1
            hold_since = time
        for _ in range(captures):
            s["caps"] += 1
            if not keep and flag:
                flag, keep = 0, True
                if hold_since >= 0:
                    s["hold"] += time - hold_since
                    hold_since = -1
        if toggle_prevent:
            if preventing and prevent_since >= 0:
                s["prevent"] += time - prevent_since
                prevent_since = -1
            elif not preventing:
                prevent_since = time
            preventing = not preventing
        if drop_pop:
            s["pops"] += 1
            if flag:
                s["drops"] += 1
                flag = 0
                if hold_since >= 0:
                    s["hold"] += time - hold_since
                    hold_since = -1
        if new_team != team:
            if not new_team:
                if in_since >= 0:
                    s["played"] += time - in_since
                    in_since = -1
                powers = 0
            flag, team = 0, new_team

    for key, since in (("played", in_since), ("hold", hold_since), ("prevent", prevent_since)):
        if since >= 0:
            s[key] += duration - since
    return s


@dataclass
class EuPlayer:
    name: str
    auth: bool
    team: int  # at the start: 1 red, 2 blue, 0 joined late
    stats: dict[str, int]


@dataclass
class EuMatch:
    id: int
    date: float
    finished: bool
    map_name: str
    team_names: tuple[str, str]
    scores: tuple[int, int]
    players: list[EuPlayer] = field(default_factory=list)


def parse_match(eu_id: int, data: dict) -> EuMatch:
    duration = int(data.get("duration") or 0)
    teams = data.get("teams") or [{}, {}]
    return EuMatch(
        id=eu_id, date=float(data.get("date") or 0), finished=bool(data.get("finished")),
        map_name=str((data.get("map") or {}).get("name") or ""),
        team_names=(str(teams[0].get("name") or ""), str(teams[1].get("name") or "")),
        scores=(int(teams[0].get("score") or 0), int(teams[1].get("score") or 0)),
        players=[EuPlayer(name=str(p.get("name") or ""), auth=bool(p.get("auth")), team=int(p.get("team") or 0),
                          stats=player_stats(p.get("events") or "", int(p.get("team") or 0), duration))
                 for p in data.get("players") or []])


# ---------- deciding whether a tagpro.eu game is ours ----------
@dataclass
class Rostered:
    player_id: int
    name: str
    team: str  # red | blue
    is_captain: bool
    discord_id: str | None = None


@dataclass
class Job:
    lobby_id: int
    started_at: float
    map_name: str | None
    team_name: str
    roster: list[Rostered]
    checked: set[int]
    hints: list[int]
    ask_captains: bool = False


@dataclass
class Found:
    eu_id: int
    red_score: int
    blue_score: int
    stats: dict[int, dict[str, int]]  # player id -> totals in seconds / counts


def _key(name: str) -> str:
    return name.strip().lower()


def evaluate(job: Job, m: EuMatch) -> Found | str:
    """The result if this tagpro.eu game is the lobby's, otherwise why not."""
    if not m.finished:
        return "the game didn't finish"
    if m.date < job.started_at - 600 or m.date > job.started_at + GIVE_UP_HOURS * 3600:
        return "played at another time"
    ours = {_key(r.name): r for r in job.roster}
    stats: dict[int, dict[str, int]] = {}
    same = flipped = 0
    for p in m.players:
        r = ours.get(_key(p.name))
        if not r:
            continue
        acc = stats.setdefault(r.player_id, dict.fromkeys(STAT_KEYS, 0))
        for k in STAT_KEYS:
            acc[k] += p.stats[k]
        if p.team in (1, 2):
            if (p.team == 1) == (r.team == "red"):
                same += 1
            else:
                flipped += 1
    if len(stats) < min(MIN_ROSTER, len(job.roster)):
        return f"only {len(stats)} of our players are in it"
    if max(same, flipped) < 2 or min(same, flipped) * 3 > max(same, flipped):
        return "can't tell which side is red"
    red, blue = m.scores if same >= flipped else m.scores[::-1]
    if red == blue:
        return "it ended in a tie"
    for acc in stats.values():
        for k in ("hold", "prevent", "played"):
            acc[k] //= FRAMES
    if job.map_name and _norm(m.map_name) != _norm(job.map_name):
        log.info("lobby %s: tagpro.eu match %s was on %r, not %r; using it anyway",
                 job.lobby_id, m.id, m.map_name, job.map_name)
    return Found(m.id, red, blue, stats)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---------- talking to tagpro.eu ----------
class Fetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport  # tests pass a MockTransport

    def client(self) -> httpx.AsyncClient:
        ua = "RankedPUGs result lookup" + (f" ({SITE_URL})" if SITE_URL else "")
        return httpx.AsyncClient(headers={"User-Agent": ua}, timeout=20, transport=self.transport,
                                 follow_redirects=True)


def _ids_on_page(html: str) -> list[int]:
    out = []
    for m in re.finditer(r"[?&]match=(\d+)", html):
        i = int(m.group(1))
        if i not in out:
            out.append(i)
    return sorted(out, reverse=True)[:PER_PAGE]  # newest first


async def candidates(client: httpx.AsyncClient, job: Job) -> list[int]:
    searches = [("team-official", job.team_name)]
    players = sorted(job.roster, key=lambda r: not r.is_captain)[:SEARCH_PLAYERS]
    searches += [("player-verified", r.name) for r in players]
    ids = list(job.hints)
    for kind, name in searches:
        try:
            r = await client.get(f"{BASE_URL}/?search={kind}&name={quote(name)}")
            if r.status_code == 200:
                ids += [i for i in _ids_on_page(r.text) if i not in ids]
        except httpx.HTTPError as e:
            log.info("tagpro.eu search %s %r failed: %s", kind, name, e)
    return [i for i in ids if i not in job.checked]


async def fetch_match(client: httpx.AsyncClient, eu_id: int) -> EuMatch | None:
    """The parsed game, or None if tagpro.eu has no such match or couldn't be reached."""
    try:
        r = await client.get(f"{BASE_URL}/data/?match={eu_id}")
        if r.status_code != 200:
            return None
        return parse_match(eu_id, r.json())
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as e:
        log.info("tagpro.eu match %s couldn't be read: %s", eu_id, e)
        return None


async def search(fetcher: Fetcher, job: Job) -> tuple[Found | None, list[int]]:
    """Look for the lobby's game. Returns what was found and the ids ruled out for good."""
    found: list[Found] = []
    ruled_out = []
    async with fetcher.client() as client:
        for eu_id in await candidates(client, job):
            m = await fetch_match(client, eu_id)
            if m is None:
                continue
            result = evaluate(job, m)
            if isinstance(result, Found):
                found.append(result)
            else:
                log.debug("lobby %s: tagpro.eu match %s isn't it: %s", job.lobby_id, eu_id, result)
                ruled_out.append(eu_id)
    # If the teams played more than once, the first finished game counts.
    return (min(found, key=lambda f: f.eu_id) if found else None), ruled_out


# ---------- database steps (run in a thread, under play.LOCK) ----------
def game_started(db: Session, lobby: Lobby):
    """Start looking for the game on tagpro.eu. Call when the lobby gets its group link."""
    if not ENABLED or db.scalar(select(ResultLookup.id).where(ResultLookup.lobby_id == lobby.id)):
        return
    t = play.clock()
    db.add(ResultLookup(lobby_id=lobby.id, status="searching", started_at=t, next_try=t + FIRST_CHECK_SECONDS,
                        checked=[], hints=[], asked_captains=False, error=""))


def lookup(db: Session, lobby_id: int) -> ResultLookup | None:
    return db.scalar(select(ResultLookup).where(ResultLookup.lobby_id == lobby_id))


def captains_wait_until(db: Session, lobby: Lobby) -> float | None:
    """While the tagpro.eu search is still in its first WAIT_MINUTES, the time captains can report from."""
    lk = lookup(db, lobby.id) if ENABLED else None
    if not lk or lk.status != "searching" or lobby.status not in UNSETTLED:
        return None
    opens = lk.started_at + WAIT_MINUTES * 60
    return opens if play.clock() < opens else None


def add_hint(db: Session, lobby: Lobby, eu_id: int):
    lk = lookup(db, lobby.id)
    if not lk:
        game_started(db, lobby)
        db.flush()
        lk = lookup(db, lobby.id)
    lk.hints = [*[h for h in lk.hints if h != eu_id], eu_id]
    lk.checked = [c for c in lk.checked if c != eu_id]
    lk.status, lk.next_try = "searching", 0


def _job(db: Session, lobby: Lobby, lk: ResultLookup, ask: bool) -> Job:
    rows = db.execute(select(LobbyPlayer, Player).join(Player, LobbyPlayer.player_id == Player.id)
                      .where(LobbyPlayer.lobby_id == lobby.id)).all()
    return Job(lobby_id=lobby.id, started_at=lk.started_at,
               map_name=db.get(Map, lobby.map_id).name if lobby.map_id else None,
               team_name=team_name(lobby.id),
               roster=[Rostered(p.id, p.tagpro_name, lp.team, lp.is_captain, p.discord_id) for lp, p in rows],
               checked=set(lk.checked), hints=list(lk.hints), ask_captains=ask)


def claim_jobs() -> list[Job]:
    t = play.clock()
    with play.LOCK, SessionLocal() as db:
        jobs = []
        for lk in db.scalars(select(ResultLookup).where(ResultLookup.status.in_(("searching", "gave_up")))).all():
            lobby = db.get(Lobby, lk.lobby_id)
            if lobby.status not in UNSETTLED:
                lk.status = "closed"
                continue
            if lk.status == "gave_up" or lk.next_try > t:
                continue
            if t > lk.started_at + GIVE_UP_HOURS * 3600:
                lk.status = "gave_up"
                continue
            ask = not lk.asked_captains and t >= lk.started_at + WAIT_MINUTES * 60
            if ask:
                lk.asked_captains = True
            lk.next_try = t + POLL_SECONDS
            jobs.append(_job(db, lobby, lk, ask))
        db.commit()
        return jobs


def save_ruled_out(lobby_id: int, ids: list[int]):
    if not ids:
        return
    with play.LOCK, SessionLocal() as db:
        lk = lookup(db, lobby_id)
        lk.checked = sorted(set(lk.checked) | set(ids))[-200:]
        lk.hints = [h for h in lk.hints if h not in ids]
        db.commit()


def save_found(job: Job, found: Found) -> bool:
    """Finish the match with the tagpro.eu score and store the stats, unless it was settled meanwhile."""
    from .reporting import finish  # imports play, like this module
    with play.LOCK, SessionLocal() as db:
        lobby = db.get(Lobby, job.lobby_id)
        lk = lookup(db, job.lobby_id)
        if lobby.status not in UNSETTLED:
            lk.status = "closed"
            db.commit()
            return False
        m = finish(db, lobby, found.red_score, found.blue_score, "tagpro.eu")
        for player_id, s in found.stats.items():
            db.add(PlayerStat(match_id=m.id, player_id=player_id, season_id=lobby.season_id,
                              tagpro_eu_id=found.eu_id, **s))
        lk.status, lk.tagpro_eu_id = "found", found.eu_id
        db.commit()
    play.bump()
    return True


# ---------- stats for the site ----------
def season_stats(db: Session, season_id: int, player_ids: list[int]) -> dict[int, dict]:
    """Season totals from tagpro.eu-read matches: {player id: {games, caps, returns, hold}}."""
    rows = db.execute(select(PlayerStat.player_id, func.count(), func.sum(PlayerStat.caps),
                             func.sum(PlayerStat.returns), func.sum(PlayerStat.hold))
                      .where(PlayerStat.season_id == season_id, PlayerStat.player_id.in_(player_ids))
                      .group_by(PlayerStat.player_id)).all()
    return {pid: {"games": n, "caps": int(c or 0), "returns": int(r or 0), "hold": int(h or 0)}
            for pid, n, c, r, h in rows}


# ---------- the worker ----------
def ask_message(job: Job) -> str:
    where = f" at {SITE_URL}/#play" if SITE_URL else " on the match page"
    return (f"**Ranked PUGs match #{job.lobby_id}:** we couldn't find this game on tagpro.eu, so please "
            f"report the final score{where}. You can queue again once both captains' scores match.")


async def handle(job: Job, fetcher: Fetcher, notifier) -> bool:
    found, ruled_out = await search(fetcher, job)
    await asyncio.to_thread(save_ruled_out, job.lobby_id, ruled_out)
    if found:
        log.info("lobby %s: tagpro.eu match %s says red %s - %s blue", job.lobby_id, found.eu_id,
                 found.red_score, found.blue_score)
        if await asyncio.to_thread(save_found, job, found):
            return True
    if job.ask_captains and notifier is not None:
        for r in job.roster:
            if r.is_captain and r.discord_id:
                await notifier.send_dm(r.discord_id, ask_message(job))
    return False


async def run_once(fetcher: Fetcher, notifier=None) -> int:
    jobs = await asyncio.to_thread(claim_jobs)
    for job in jobs:  # one at a time, to go easy on tagpro.eu
        await handle(job, fetcher, notifier)
    return len(jobs)


async def worker(fetcher: Fetcher | None = None, notifier=None, interval: float = 15):
    from .discord_bot import DiscordNotifier
    fetcher = fetcher or Fetcher()
    notifier = notifier or DiscordNotifier()
    while True:
        try:
            await run_once(fetcher, notifier)
        except Exception:
            log.exception("tagpro.eu worker failed")
        await asyncio.sleep(interval)


def start() -> asyncio.Task | None:
    """Start the worker if TAGPRO_EU is on (the default). Called from the app's lifespan."""
    return asyncio.create_task(worker()) if ENABLED else None
