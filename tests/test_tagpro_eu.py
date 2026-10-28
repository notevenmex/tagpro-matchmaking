import asyncio
import base64
import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SESSION_SECRET", "test-secret")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from conftest import client  # noqa: E402
from app import play, tagpro_eu  # noqa: E402
from app.db import (Lobby, LobbyPlayer, Match, Player, PlayerStat, ResultLookup, Season,  # noqa: E402
                    SessionLocal)
from app.tagpro_group import group_settings  # noqa: E402


# ---------- a tiny encoder for tagpro.eu event blobs, to test the decoder ----------
class BitWriter:
    def __init__(self):
        self.bits = []

    def bit(self, b):
        self.bits.append(1 if b else 0)

    def fixed(self, v, n):
        for i in reversed(range(n)):
            self.bit(v >> i & 1)

    def tally(self, n):
        for _ in range(n):
            self.bit(1)
        self.bit(0)

    def footer(self, v):
        free = (8 - ((len(self.bits) + 2) & 7)) & 7
        for s in range(4):
            size, minimum, f = s << 3 | free, 0, free
            while f < size:
                minimum += 1 << f
                f += 8
            if 0 <= v - minimum < (1 << size):
                self.fixed(s, 2)
                self.fixed(v - minimum, size)
                return
        raise ValueError(v)

    def b64(self):
        bits = self.bits + [0] * (-len(self.bits) % 8)
        data = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
        return base64.b64encode(data).decode()


def events(ticks):
    """ticks: (frame, {"grab", "cap", "returns", "tags", "drop"}) for a player who stays on one team."""
    w, last, flag = BitWriter(), -1, False
    for frame, e in ticks:
        w.bit(0)                      # no team change
        w.bit(e.get("drop", False))   # drop or pop
        w.tally(e.get("returns", 0))
        w.tally(e.get("tags", 0))
        grab = e.get("grab", False)
        if not flag:
            w.bit(grab)
        caps = e.get("cap", 0)
        w.tally(caps)
        if caps and not e.get("drop") and (flag or grab):
            w.bit(0)                  # the capture uses up the flag
        if grab:
            w.fixed(0, 2)             # the opponent's flag
        w.tally(0)                    # no powerups
        w.bit(0), w.bit(0), w.bit(0)  # prevent, button, block toggles
        w.footer(frame - last - 1)
        last = frame
        if grab:
            flag = True
        if caps or e.get("drop"):
            flag = False
    return w.b64()


def test_decodes_caps_returns_and_hold():
    blob = events([(600, {"grab": True}), (2400, {"cap": 1}), (3000, {"returns": 2, "tags": 1}),
                   (3600, {"grab": True}), (4200, {"drop": True}), (40000, {"tags": 1})])
    s = tagpro_eu.player_stats(blob, 1, 36000 + 6000)
    assert s["caps"] == 1 and s["grabs"] == 2 and s["drops"] == 1 and s["pops"] == 1
    assert s["returns"] == 2 and s["tags"] == 4
    assert s["hold"] == (2400 - 600) + (4200 - 3600)
    assert s["played"] == 42000


def test_parse_match_links():
    assert tagpro_eu.parse_match_id("https://tagpro.eu/?match=4356390") == 4356390
    assert tagpro_eu.parse_match_id("tagpro.eu/?match=12") is None
    assert tagpro_eu.parse_match_id("https://tagpro.eu/data/?match=77") == 77
    assert tagpro_eu.parse_match_id(" 123 ") == 123
    assert tagpro_eu.parse_match_id("https://evil.example/?match=1") is None


def test_groups_and_teams_are_named_after_the_match():
    s = group_settings(42)
    assert s["redTeamName"] == s["blueTeamName"] == "PUG42"
    assert tagpro_eu.team_name(42) == "PUG42"


# ---------- the lookup, end to end ----------
_n = 0


@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(play, "clock", lambda: now[0])
    return now


def started_lobby():
    """An 8-player lobby whose group link just went up. Returns (lobby id, roster names, captain ids)."""
    global _n
    _n += 1
    with play.LOCK, SessionLocal() as db:
        season = db.scalar(select(Season).where(Season.is_active.is_(True)))
        lobby = Lobby(season_id=season.id, status="in_progress", created_at=play.clock(), map_pool=[], map_id=1,
                      server="US East", group_link="https://tagpro.koalabeast.com/groups/abcdefgh")
        db.add(lobby)
        db.flush()
        names = {"red": [], "blue": []}
        caps = {}
        for i in range(8):
            team = "red" if i % 2 == 0 else "blue"
            p = Player(discord_id=f"eu{_n}-{i}", tagpro_name=f"Eu{_n}x{i}", position="offense", tags=[])
            db.add(p)
            db.flush()
            db.add(LobbyPlayer(lobby_id=lobby.id, player_id=p.id, joined_at=i, elo=1000, accepted=True,
                               team=team, is_captain=i < 2))
            names[team].append(p.tagpro_name)
            if i < 2:
                caps[team] = p.discord_id
        tagpro_eu.game_started(db, lobby)
        db.commit()
        return lobby.id, names, caps


def eu_game(start, names, red_on, red_score, blue_score, finished=True):
    """tagpro.eu JSON for a game where our red team played as TagPro team red_on (1 or 2)."""
    blob = events([(600, {"grab": True}), (1200, {"cap": 1}), (1500, {"returns": 1})])
    players = [{"name": n.upper() if i == 0 else n, "auth": True, "team": red_on, "events": blob}
               for i, n in enumerate(names["red"])]
    players += [{"name": n, "auth": True, "team": 3 - red_on, "events": ""} for n in names["blue"]]
    scores = [red_score, blue_score] if red_on == 1 else [blue_score, red_score]
    return {"server": "tagpro-newcastle.koalabeast.com", "port": 8000, "official": True, "group": "x",
            "date": start + 120, "timeLimit": 10, "duration": 36000, "finished": finished,
            "map": {"name": "Bombing Run"}, "players": players,
            "teams": [{"name": "PUG", "score": scores[0]}, {"name": "PUG", "score": scores[1]}]}


class FakeEU:
    def __init__(self):
        self.pages: dict[str, list[int]] = {}
        self.games: dict[int, dict] = {}
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        q = request.url.params
        if request.url.path.startswith("/data"):
            game = self.games.get(int(q["match"]))
            return httpx.Response(200, json=game) if game else httpx.Response(404)
        ids = self.pages.get(q.get("name"), [])
        html = "<table>" + "".join(f'<tr><td><a href="?match={i}">{i}</a></td></tr>' for i in ids) + "</table>"
        return httpx.Response(200, text=html)

    def fetcher(self):
        return tagpro_eu.Fetcher(transport=httpx.MockTransport(self.handler))


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send_dm(self, user_id, content):
        self.sent.append((user_id, content))
        return True


def lookup_of(lid):
    with SessionLocal() as db:
        return db.scalar(select(ResultLookup).where(ResultLookup.lobby_id == lid))


def test_tagpro_eu_settles_the_match_and_records_stats(clock):
    start = clock[0]
    lid, names, caps = started_lobby()
    red_cap = client(caps["red"])

    # captains wait for tagpro.eu instead of reporting
    assert "tagpro.eu" in red_cap.get("/api/play").json()["queue_block"]
    assert red_cap.post(f"/api/lobbies/{lid}/report", json={"red_score": 3, "blue_score": 1}).status_code == 409
    score = red_cap.get(f"/api/lobbies/{lid}/score").json()
    assert score["can_report"] is False and score["tagpro_eu"]["captains_wait_until"] == start + 25 * 60

    eu = FakeEU()
    eu.pages[f"PUG{lid}"] = [900, 901]
    eu.games[900] = eu_game(start - 3600, names, 1, 5, 0)    # an older game: wrong time
    eu.games[901] = eu_game(start, names, 2, 4, 2)          # ours, with our red team on TagPro's blue
    assert asyncio.run(tagpro_eu.run_once(eu.fetcher())) == 0  # too early to look
    clock[0] += 10 * 60
    assert asyncio.run(tagpro_eu.run_once(eu.fetcher(), FakeNotifier())) == 1

    with SessionLocal() as db:
        lobby = db.get(Lobby, lid)
        m = db.get(Match, lobby.match_id)
        assert lobby.status == "finished" and m.reported_by == "tagpro.eu"
        assert (m.red_score, m.blue_score, m.winner) == (4, 2, "red")
        stats = {s.player_id: s for s in db.scalars(select(PlayerStat).where(PlayerStat.match_id == m.id))}
        assert len(stats) == 8
        first_red = db.scalar(select(Player).where(Player.tagpro_name == names["red"][0]))
        assert stats[first_red.id].caps == 1 and stats[first_red.id].returns == 1 and stats[first_red.id].hold == 10
    lk = lookup_of(lid)
    assert lk.status == "found" and lk.tagpro_eu_id == 901 and 900 in lk.checked

    final = red_cap.get(f"/api/lobbies/{lid}/score").json()["final"]
    assert final["verified_by"] == "tagpro.eu" and final["tagpro_eu"].endswith("?match=901")
    assert red_cap.get("/api/play").json()["queue_block"] is None
    view = red_cap.get(f"/api/lobbies/{lid}").json()
    me = next(p for p in view["players"] if p["name"] == names["red"][0])
    assert me["stats"] == {"games": 1, "caps": 1, "returns": 1, "hold": 10}


def test_captains_report_when_tagpro_eu_has_nothing(clock):
    lid, names, caps = started_lobby()
    eu, notifier = FakeEU(), FakeNotifier()
    eu.pages[names["red"][0]] = [950]
    eu.games[950] = {**eu_game(clock[0], names, 1, 3, 1), "players": []}  # a captain's game without us
    clock[0] += 10 * 60
    asyncio.run(tagpro_eu.run_once(eu.fetcher(), notifier))
    assert lookup_of(lid).status == "searching" and not notifier.sent and 950 in lookup_of(lid).checked

    clock[0] += 16 * 60  # past the 25 minute wait
    asyncio.run(tagpro_eu.run_once(eu.fetcher(), notifier))
    assert sorted(u for u, _ in notifier.sent) == sorted(caps.values())  # both captains asked once
    assert sum(1 for u in eu.requests if "data" in u and "950" in u) == 1  # not fetched again
    red_cap, blue_cap = client(caps["red"]), client(caps["blue"])
    assert red_cap.get(f"/api/lobbies/{lid}/score").json()["can_report"] is True
    assert red_cap.post(f"/api/lobbies/{lid}/report", json={"red_score": 3, "blue_score": 1}).json()["result"] == "waiting"
    assert blue_cap.post(f"/api/lobbies/{lid}/report", json={"red_score": 3, "blue_score": 1}).json()["result"] == "verified"

    clock[0] += 2 * 60
    asyncio.run(tagpro_eu.run_once(eu.fetcher(), notifier))
    assert lookup_of(lid).status == "closed" and len(notifier.sent) == 2


def test_a_pasted_tagpro_eu_link_is_checked(clock):
    lid, names, caps = started_lobby()
    other = client(f"eu{_n}-5")
    assert other.post(f"/api/lobbies/{lid}/tagpro-eu", json={"link": "https://tagpro.eu/?match=990"}).status_code == 403
    red_cap = client(caps["red"])
    assert red_cap.post(f"/api/lobbies/{lid}/tagpro-eu", json={"link": "nope"}).status_code == 400
    assert red_cap.post(f"/api/lobbies/{lid}/tagpro-eu",
                        json={"link": "https://tagpro.eu/?match=990"}).status_code == 200
    eu = FakeEU()
    eu.games[990] = eu_game(clock[0], names, 1, 1, 3)
    asyncio.run(tagpro_eu.run_once(eu.fetcher()))  # a pasted link is checked right away
    with SessionLocal() as db:
        m = db.get(Match, db.get(Lobby, lid).match_id)
        assert (m.red_score, m.blue_score, m.winner, m.reported_by) == (1, 3, "blue", "tagpro.eu")
