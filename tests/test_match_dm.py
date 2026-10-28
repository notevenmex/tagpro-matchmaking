import asyncio

import pytest
from conftest import client
from sqlalchemy import select

from app import match_dm, play
from app.db import GroupJob, Lobby, LobbyPlayer, Map, MatchNotice, Player, Season, SessionLocal, SiteText, VoiceRoom


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    t = [9_000_000.0]
    monkeypatch.setattr(play, "clock", lambda: t[0])
    clear()
    yield t
    clear()


def clear():
    with SessionLocal() as db:  # other test files leave live lobbies behind
        for lobby in db.scalars(select(Lobby).where(Lobby.status != "cancelled")).all():
            lobby.status = "cancelled"
        for key in ("discord_invite", "match_rules"):
            if row := db.get(SiteText, key):
                db.delete(row)
        db.commit()


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send_dm(self, user_id, content):
        self.sent.append((user_id, content))
        return True


def make_lobby(created_at=9_000_000.0, link="https://tagpro.koalabeast.com/groups/abc", bot_group=True) -> tuple[int, dict]:
    """An in-progress 2v2 with voice channels. One blue player has no Discord."""
    with SessionLocal() as db:
        s = db.scalar(select(Season).where(Season.is_active.is_(True)))
        n = db.scalar(select(Lobby.id).order_by(Lobby.id.desc()).limit(1)) or 0
        lobby = Lobby(season_id=s.id, status="in_progress", created_at=created_at, map_pool=[], server="US East",
                      group_link=link, map_id=db.scalar(select(Map.id).limit(1)))
        db.add(lobby)
        db.flush()
        ids = {}
        for i, (team, did, cap) in enumerate((("red", f"m{n}-r1", True), ("red", f"m{n}-r2", False),
                                               ("blue", f"m{n}-b1", True), ("blue", None, False))):
            p = Player(discord_id=did, tagpro_name=f"M{n}P{i}", position="offense", tags=[])
            db.add(p)
            db.flush()
            db.add(LobbyPlayer(lobby_id=lobby.id, player_id=p.id, joined_at=0, elo=1000, accepted=True,
                               team=team, is_captain=cap, pick_no=None if cap else i))
            ids[p.tagpro_name] = did
        for team in ("red", "blue"):
            db.add(VoiceRoom(lobby_id=lobby.id, team=team, channel_id=f"vc-{team}-{lobby.id}", created_at=0))
        if bot_group:
            db.add(GroupJob(lobby_id=lobby.id, status="done", attempts=1, next_try=0, error="", dms_sent=0))
        db.commit()
        return lobby.id, ids


def test_every_player_gets_one_match_ready_dm():
    lid, ids = make_lobby()
    n = FakeNotifier()
    asyncio.run(match_dm.run_once(n))
    dms = dict(n.sent)
    assert set(dms) == {i for i in ids.values() if i}  # everyone with Discord, nobody twice
    red = next(v for k, v in dms.items() if k.endswith("-r2"))
    assert "Match ready" in red and "https://tagpro.koalabeast.com/groups/abc" in red
    assert f"vc-red-{lid}" in red and f"vc-blue-{lid}" not in red
    assert "1. VC Required" in red and "2. Don't be toxic" in red
    assert "captain" not in red.split("\n")[1]  # not a captain
    cap = next(v for k, v in dms.items() if k.endswith("-r1"))
    assert "as captain" in cap and "group leader" in cap

    asyncio.run(match_dm.run_once(n))  # already sent
    assert len(n.sent) == 3
    with SessionLocal() as db:
        assert db.scalar(select(MatchNotice.sent).where(MatchNotice.lobby_id == lid)) == 3


def test_admin_invite_and_rules_are_used():
    admin = client("admin-1")
    assert client("someone").get("/api/match-dm").status_code == 403
    r = admin.get("/api/match-dm").json()
    assert r["rule_lines"] == ["VC Required", "Don't be toxic"]
    assert admin.put("/api/match-dm", json={"discord_invite": "not a url", "rules": ""}).status_code == 422
    r = admin.put("/api/match-dm", json={"discord_invite": "https://discord.gg/pugs",
                                         "rules": "1. VC Required\n2. Don't be toxic\n- No smurfing\n"}).json()
    assert r["rule_lines"] == ["VC Required", "Don't be toxic", "No smurfing"]

    make_lobby(bot_group=False)
    n = FakeNotifier()
    asyncio.run(match_dm.run_once(n))
    text = n.sent[0][1]
    assert "💬 Discord: https://discord.gg/pugs" in text and "3. No smurfing" in text
    assert all("group leader" not in t for _, t in n.sent)  # a captain made this group by hand


def test_corrected_link_is_resent():
    lid, _ = make_lobby()
    n = FakeNotifier()
    asyncio.run(match_dm.run_once(n))
    with SessionLocal() as db:
        db.get(Lobby, lid).group_link = "https://tagpro.koalabeast.com/groups/fixed"
        db.commit()
    asyncio.run(match_dm.run_once(n))
    resent = n.sent[3:]
    assert len(resent) == 3 and all("updated group link" in t and "/groups/fixed" in t for _, t in resent)


def test_waits_for_the_link_and_skips_old_games_on_startup(setup):
    t = setup
    waiting, _ = make_lobby(link=None)
    old, _ = make_lobby(created_at=t[0] - 2 * match_dm.STALE_SECONDS)
    with SessionLocal() as db:
        db.get(Lobby, waiting).status = "awaiting_group"
        db.commit()
    match_dm.skip_old_lobbies()
    n = FakeNotifier()
    asyncio.run(match_dm.run_once(n))
    assert not n.sent
