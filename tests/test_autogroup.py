import asyncio
import json
import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SESSION_SECRET", "test-secret")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import autogroup, play, seed  # noqa: E402
from app.db import GroupJob, Lobby, LobbyPlayer, Map, Player, Season, SessionLocal  # noqa: E402
from app.discord_bot import DiscordNotifier  # noqa: E402
from app.tagpro_group import GroupError, group_settings, pick_map, pick_server  # noqa: E402

seed.run()


@pytest.fixture(autouse=True)
def cancel_leftovers():
    """Don't leave half-finished lobbies behind for the other test files."""
    yield
    with SessionLocal() as db:
        for lobby in db.scalars(select(Lobby).where(Lobby.status != "cancelled")).all():
            lobby.status = "cancelled"
        db.commit()


def make_lobby(status="awaiting_group") -> int:
    """A lobby that has finished the draft, veto and server vote, with two captains (one without Discord)."""
    with SessionLocal() as db:
        s = db.scalar(select(Season).where(Season.is_active.is_(True)))
        n = db.scalar(select(Lobby.id).order_by(Lobby.id.desc()).limit(1)) or 0
        red = Player(discord_id=f"red-{n}", tagpro_name=f"RedCap{n}", position="offense", tags=[])
        blue = Player(discord_id=None, tagpro_name=f"BlueCap{n}", position="defense", tags=[])
        db.add_all([red, blue])
        lobby = Lobby(season_id=s.id, status=status, created_at=0, map_pool=[], server="US Central",
                      map_id=db.scalar(select(Map.id).where(Map.name == "Moon Base 2024")))
        db.add(lobby)
        db.flush()
        for p, team in ((red, "red"), (blue, "blue")):
            db.add(LobbyPlayer(lobby_id=lobby.id, player_id=p.id, joined_at=0, elo=1500, accepted=True,
                               team=team, is_captain=True))
        db.commit()
        return lobby.id


class FakeCreator:
    def __init__(self, fail=0):
        self.fail, self.specs = fail, []

    async def create(self, spec):
        self.specs.append(spec)
        if self.fail:
            self.fail -= 1
            raise GroupError("TagPro asked for a captcha when creating the group.")
        return f"https://tagpro.koalabeast.com/groups/abcdefg{spec.lobby_id % 10}"


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send_dm(self, user_id, content):
        self.sent.append((user_id, content))
        return True


def lobby_and_job(lobby_id):
    with SessionLocal() as db:
        return db.get(Lobby, lobby_id), db.scalar(select(GroupJob).where(GroupJob.lobby_id == lobby_id))


def test_creates_group_and_leaves_dms_to_match_dm():
    lid = make_lobby()
    other = make_lobby(status="in_progress")  # already has a group: left alone
    creator, notifier = FakeCreator(), FakeNotifier()
    asyncio.run(autogroup.run_once(creator, notifier))
    spec = next(s for s in creator.specs if s.lobby_id == lid)
    assert (spec.map_name, spec.server) == ("Moon Base 2024", "US Central")
    assert [c.team for c in spec.captains] == ["red", "blue"]
    assert all(s.lobby_id != other for s in creator.specs)

    lobby, job = lobby_and_job(lid)
    assert lobby.status == "in_progress" and lobby.group_link.startswith("https://tagpro.koalabeast.com/groups/")
    assert job.status == "done" and not notifier.sent  # the match ready DM (match_dm.py) carries the link

    asyncio.run(autogroup.run_once(creator, notifier))  # nothing left to do
    assert sum(s.lobby_id == lid for s in creator.specs) == 1


def test_retries_then_falls_back_to_manual(monkeypatch):
    monkeypatch.setattr(autogroup, "MAX_ATTEMPTS", 2)
    t = [1000.0]
    monkeypatch.setattr(play, "clock", lambda: t[0])
    lid = make_lobby()
    creator, notifier = FakeCreator(fail=5), FakeNotifier()

    asyncio.run(autogroup.run_once(creator, notifier))
    lobby, job = lobby_and_job(lid)
    assert job.status == "retry" and "captcha" in job.error and not notifier.sent
    asyncio.run(autogroup.run_once(creator, notifier))  # too early to retry
    assert job.attempts == 1 == sum(s.lobby_id == lid for s in creator.specs)

    t[0] += autogroup.RETRY_SECONDS + 1
    asyncio.run(autogroup.run_once(creator, notifier))
    lobby, job = lobby_and_job(lid)
    assert job.status == "failed" and lobby.status == "awaiting_group" and lobby.group_link is None
    assert len(notifier.sent) == 1 and "couldn't create" in notifier.sent[0][1]


def test_cancelled_lobby_gets_no_dm():
    lid = make_lobby()

    class CancelWhileCreating(FakeCreator):
        async def create(self, spec):
            with SessionLocal() as db:
                db.get(Lobby, spec.lobby_id).status = "cancelled"
                db.commit()
            return await super().create(spec)

    notifier = FakeNotifier()
    asyncio.run(autogroup.run_once(CancelWhileCreating(), notifier))
    lobby, job = lobby_and_job(lid)
    assert lobby.status == "cancelled" and lobby.group_link is None and not notifier.sent


def test_settings_and_option_matching(monkeypatch):
    monkeypatch.setenv("TAGPRO_NOKISS_SETTING", "noKiss=true")
    monkeypatch.setenv("TAGPRO_GROUP_SETTINGS", json.dumps({"time": 8}))
    s = group_settings()
    assert s["noKiss"] == "true" and s["time"] == "8" and s["caps"] == "0" and s["mode"] == "classic"

    maps = [("", "Random"), ("bomber", "Bombing Run"), ("Pilot", "Pilot"), ("Mapache Chief", "Mapache Chief")]
    assert pick_map(maps, "Bombing Run") == "bomber"
    assert pick_map(maps, "pilot") == "Pilot"
    assert pick_map(maps, "Mapache Chief") == "Mapache Chief"
    assert pick_map(maps, "Nowhere") is None

    servers = [("", "Any"), ("tagpro-chicago", "Chicago"), ("tagpro-newcastle", "Newcastle")]
    assert pick_server(servers, "US Central") == "tagpro-chicago"
    assert pick_server(servers, "Europe") is None
    monkeypatch.setenv("TAGPRO_SERVER_MAP", json.dumps({"Europe": "Newcastle"}))
    assert pick_server(servers, "Europe") == "tagpro-newcastle"


def test_discord_dm_uses_bot_api():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content), req.headers["authorization"]))
        if req.url.path.endswith("/users/@me/channels"):
            return httpx.Response(200, json={"id": "dm-1"})
        if req.url.path.endswith("/channels/dm-1/messages"):
            return httpx.Response(200, json={"id": "m-1"})
        return httpx.Response(404)

    n = DiscordNotifier("tok", transport=httpx.MockTransport(handler))
    assert asyncio.run(n.send_dm("42", "hello"))
    assert calls[0][1:] == ("/api/v10/users/@me/channels", {"recipient_id": "42"}, "Bot tok")
    assert calls[1][2]["content"] == "hello"

    closed = DiscordNotifier("tok", transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"code": 50007}) if "messages" in r.url.path
        else httpx.Response(200, json={"id": "dm-1"})))
    assert not asyncio.run(closed.send_dm("42", "hi"))
    assert not asyncio.run(DiscordNotifier("").send_dm("42", "hi"))
