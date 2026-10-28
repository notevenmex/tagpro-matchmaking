import asyncio
import json
import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("DEV_LOGIN", "1")  # the same settings as test_play.py, whichever file is imported first
os.environ.setdefault("ADMIN_DISCORD_IDS", "admin-1")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import play, seed, voice  # noqa: E402
from app.db import Lobby, LobbyPlayer, Player, Season, SessionLocal, VoiceRoom  # noqa: E402
from app.discord_bot import DiscordNotifier  # noqa: E402

seed.run()


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    t = [5_000_000.0]
    monkeypatch.setattr(play, "clock", lambda: t[0])
    monkeypatch.setattr(voice, "GUILD_ID", "guild-1")
    monkeypatch.setattr(voice, "CATEGORY_ID", "cat-1")
    monkeypatch.setattr(voice, "LOBBY_CHANNEL_ID", "")
    monkeypatch.setattr(voice, "STAFF_ROLE_ID", "staff-1")
    voice._retry_at.clear()
    clear_lobbies()  # other test files leave live lobbies behind
    yield t
    clear_lobbies()


def clear_lobbies():
    with SessionLocal() as db:
        for lobby in db.scalars(select(Lobby).where(Lobby.status != "cancelled")).all():
            lobby.status = "cancelled"
        for row in db.scalars(select(VoiceRoom)).all():
            row.deleted = True
        db.commit()


def make_lobby(status="map_veto") -> tuple[int, dict]:
    """A drafted 2v2 lobby. Returns its id and {team: [discord ids]}; one blue player has no Discord."""
    with SessionLocal() as db:
        s = db.scalar(select(Season).where(Season.is_active.is_(True)))
        n = db.scalar(select(Lobby.id).order_by(Lobby.id.desc()).limit(1)) or 0
        lobby = Lobby(season_id=s.id, status=status, created_at=0, map_pool=[])
        db.add(lobby)
        db.flush()
        teams = {"red": [], "blue": []}
        for i, (team, did) in enumerate((("red", f"v{n}-r1"), ("red", f"v{n}-r2"), ("blue", f"v{n}-b1"),
                                          ("blue", None))):
            p = Player(discord_id=did, tagpro_name=f"V{n}P{i}", position="offense", tags=[])
            db.add(p)
            db.flush()
            db.add(LobbyPlayer(lobby_id=lobby.id, player_id=p.id, joined_at=0, elo=1500, accepted=True,
                               team=team, is_captain=i in (0, 2), pick_no=None if i in (0, 2) else i))
            if did:
                teams[team].append(did)
        db.commit()
        return lobby.id, teams


def set_status(lobby_id, status):
    with SessionLocal() as db:
        db.get(Lobby, lobby_id).status = status
        db.commit()


def rooms(lobby_id):
    with SessionLocal() as db:
        return {r.team: r for r in db.scalars(select(VoiceRoom).where(VoiceRoom.lobby_id == lobby_id)).all()}


class FakeDiscord:
    """Just enough of Discord's API: channels, voice states and member moves."""

    def __init__(self, in_voice=(), fail_create=0):
        self.calls, self.dms, self.channels = [], [], {}
        self.voice = {uid: "waiting-room" for uid in in_voice}
        self.fail_create, self.token = fail_create, "tok"

    async def request(self, method, path, body=None, reason=""):
        self.calls.append((method, path, body))
        if path == "/users/@me":
            return httpx.Response(200, json={"id": "bot-1"})
        if method == "POST" and path == "/guilds/guild-1/channels":
            if self.fail_create:
                self.fail_create -= 1
                return httpx.Response(403, json={"code": 50013, "message": "Missing Permissions"})
            cid = f"ch-{len(self.channels) + 1}"
            self.channels[cid] = {"id": cid, "type": 2, **body}
            return httpx.Response(201, json=self.channels[cid])
        if method == "GET" and path == "/guilds/guild-1/channels":
            return httpx.Response(200, json=list(self.channels.values()))
        if method == "GET" and "/voice-states/" in path:
            uid = path.rsplit("/", 1)[1]
            return (httpx.Response(200, json={"channel_id": self.voice[uid]}) if uid in self.voice
                    else httpx.Response(404, json={"code": 10065}))
        if method == "PATCH" and "/members/" in path:
            uid = path.rsplit("/", 1)[1]
            if uid not in self.voice:
                return httpx.Response(400, json={"code": 40032})  # target user is not connected to voice
            self.voice[uid] = body["channel_id"]
            return httpx.Response(200, json={})
        if method == "DELETE" and path.startswith("/channels/"):
            cid = path.rsplit("/", 1)[1]
            if self.channels.pop(cid, None) is None:
                return httpx.Response(404, json={"code": 10003})
            for uid, ch in list(self.voice.items()):
                if ch == cid:
                    del self.voice[uid]
            return httpx.Response(200, json={})
        return httpx.Response(404)

    async def send_dm(self, user_id, content):
        self.dms.append((user_id, content))
        return True


def test_opens_private_team_channels_and_moves_players():
    lid, teams = make_lobby()
    drafting, _ = make_lobby(status="drafting")  # teams not final yet: no channels
    api = FakeDiscord(in_voice=[teams["red"][0]])
    asyncio.run(voice.run_once(api))

    r = rooms(lid)
    assert set(r) == {"red", "blue"} and all(x.channel_id for x in r.values())
    assert not rooms(drafting)
    red = api.channels[r["red"].channel_id]
    assert red["parent_id"] == "cat-1" and f"Match {lid}" in red["name"] and "Red" in red["name"]
    ow = {o["id"]: o for o in red["permission_overwrites"]}
    assert int(ow["guild-1"]["deny"]) & voice.VIEW and int(ow["guild-1"]["deny"]) & voice.CONNECT
    assert int(ow["bot-1"]["allow"]) & voice.MANAGE and int(ow["staff-1"]["allow"]) & voice.CONNECT
    assert {i for i in ow if i not in ("guild-1", "bot-1", "staff-1")} == set(teams["red"])
    blue_ow = {o["id"] for o in api.channels[r["blue"].channel_id]["permission_overwrites"]}
    assert not blue_ow & set(teams["red"])

    # the red player sitting in the waiting room got moved; everyone with Discord got a DM
    assert api.voice[teams["red"][0]] == r["red"].channel_id
    dms = dict(api.dms)
    assert set(dms) == set(teams["red"] + teams["blue"])
    assert "moved you" in dms[teams["red"][0]] and "Join your team's voice channel" in dms[teams["red"][1]]
    assert f"https://discord.com/channels/guild-1/{r['red'].channel_id}" in dms[teams["red"][1]]

    created = sum(1 for m, p, _ in api.calls if m == "POST")
    asyncio.run(voice.run_once(api))  # nothing new to do
    assert sum(1 for m, p, _ in api.calls if m == "POST") == created


def test_deletes_channels_after_the_match_and_grace(setup, monkeypatch):
    t = setup
    monkeypatch.setattr(voice, "LOBBY_CHANNEL_ID", "post-game")
    lid, teams = make_lobby(status="in_progress")
    api = FakeDiscord(in_voice=teams["blue"])
    asyncio.run(voice.run_once(api))
    blue = rooms(lid)["blue"].channel_id
    assert api.voice[teams["blue"][0]] == blue

    set_status(lid, "finished")
    asyncio.run(voice.run_once(api))
    assert len(api.channels) == 2 and rooms(lid)["red"].ended_at == t[0]  # grace period: still there

    t[0] += voice.GRACE_SECONDS
    asyncio.run(voice.run_once(api))
    assert not api.channels and all(x.deleted for x in rooms(lid).values())
    assert api.voice[teams["blue"][0]] == "post-game"  # moved out before the channel went away

    calls = len(api.calls)
    asyncio.run(voice.run_once(api))  # finished and deleted: never recreated
    assert len(api.calls) == calls


def test_cancelled_and_stale_lobbies_lose_their_channels(setup):
    t = setup
    lid, _ = make_lobby()
    stale, _ = make_lobby(status="in_progress")
    api = FakeDiscord()
    asyncio.run(voice.run_once(api))
    set_status(lid, "cancelled")
    t[0] += voice.MAX_HOURS * 3600 + 1  # the other one never got a score report
    asyncio.run(voice.run_once(api))
    t[0] += voice.GRACE_SECONDS
    asyncio.run(voice.run_once(api))
    assert not api.channels
    assert all(x.deleted for x in rooms(lid).values()) and all(x.deleted for x in rooms(stale).values())


def test_failed_create_is_retried_later(setup):
    t = setup
    lid, _ = make_lobby()
    api = FakeDiscord(fail_create=1)
    asyncio.run(voice.run_once(api))
    assert set(rooms(lid)) == {"blue"}  # red failed and was forgotten
    asyncio.run(voice.run_once(api))
    assert set(rooms(lid)) == {"blue"}  # waiting before the retry
    t[0] += voice.RETRY_SECONDS
    asyncio.run(voice.run_once(api))
    assert set(rooms(lid)) == {"red", "blue"} and len(api.channels) == 2


def test_sweep_deletes_leftover_match_channels():
    api = FakeDiscord()
    api.channels = {"old": {"id": "old", "type": 2, "parent_id": "cat-1", "name": "🔴 Match 999 · Red"},
                    "lounge": {"id": "lounge", "type": 2, "parent_id": "cat-1", "name": "Waiting Room"},
                    "elsewhere": {"id": "elsewhere", "type": 2, "parent_id": "cat-2", "name": "🔵 Match 1 · Blue"}}
    asyncio.run(voice.sweep_orphans(api))
    assert set(api.channels) == {"lounge", "elsewhere"}


def test_match_page_shows_my_team_voice_link():
    lid, teams = make_lobby()
    asyncio.run(voice.run_once(FakeDiscord()))
    with SessionLocal() as db:
        lobby = db.get(Lobby, lid)
        me = db.scalar(select(Player).where(Player.discord_id == teams["blue"][0]))
        view = play.lobby_view(db, lobby, me)
        assert view["voice_link"] == voice.channel_url(rooms(lid)["blue"].channel_id)


def test_rest_client_sends_audit_reason_and_retries_rate_limits():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.headers.get("x-audit-log-reason")))
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0})
        return httpx.Response(200, json={"id": "c"})

    api = DiscordNotifier("tok", transport=httpx.MockTransport(handler))
    r = asyncio.run(api.request("DELETE", "/channels/c", reason="match over"))
    assert r.status_code == 200 and calls == [("DELETE", "/api/v10/channels/c", "match over")] * 2
    assert json.loads(r.content) == {"id": "c"}


def test_server_setup_creates_layout_once(monkeypatch, capsys):
    from app import discord_setup
    state = {"roles": [{"id": "guild-1", "name": "@everyone"}], "channels": []}

    def handler(req: httpx.Request):
        path, body = req.url.path.removeprefix("/api/v10"), json.loads(req.content or b"null")
        if path == "/users/@me":
            return httpx.Response(200, json={"id": "bot-1"})
        kind = "roles" if path.endswith("/roles") else "channels"
        if req.method == "GET":
            return httpx.Response(200, json=state[kind])
        item = {"id": f"{kind}-{len(state[kind])}", "parent_id": None, **body}
        state[kind].append(item)
        return httpx.Response(201, json=item)

    monkeypatch.setenv("DISCORD_GUILD_ID", "guild-1")
    monkeypatch.setattr(discord_setup, "DiscordNotifier",
                        lambda: DiscordNotifier("tok", transport=httpx.MockTransport(handler)))
    assert asyncio.run(discord_setup.main()) == 0
    out = capsys.readouterr().out
    names = {c["name"] for c in state["channels"]}
    assert {"🏆 Live Matches", "Waiting Room", "rules", "bot-log"} <= names
    live = next(c for c in state["channels"] if c["name"] == "🏆 Live Matches")
    assert f"VOICE_CATEGORY_ID={live['id']}" in out
    assert {o["id"] for o in live["permission_overwrites"]} == {"guild-1", "roles-1", "bot-1"}

    count = len(state["channels"]) + len(state["roles"])
    assert asyncio.run(discord_setup.main()) == 0  # second run: nothing new
    assert len(state["channels"]) + len(state["roles"]) == count
