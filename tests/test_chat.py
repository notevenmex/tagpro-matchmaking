import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("ADMIN_DISCORD_IDS", "admin-1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import chat, play, seed  # noqa: E402
from app.db import Lobby, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

seed.run()
T = [3_000_000.0]


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(play, "clock", lambda: T[0])  # other test modules swap in their own clock


def player(i):
    c = TestClient(app)
    c.get("/auth/dev-login", params={"discord_id": f"chat-{i}", "name": f"C{i}"}, follow_redirects=False)
    if c.get("/api/me").json()["player"] is None:
        assert c.post("/api/register", json={"tagpro_name": f"Chatter{i}", "position": "offense"}).status_code == 201
    return c


def admin():
    c = TestClient(app)
    c.get("/auth/dev-login", params={"discord_id": "admin-1", "name": "Admin"}, follow_redirects=False)
    if c.get("/api/me").json()["player"] is None:
        c.post("/api/register", json={"tagpro_name": "AdminGuy", "position": "flex"})
    return c


def start_match(first):
    cs = [player(first + i) for i in range(8)]
    for c in cs:
        assert c.post("/api/queue").status_code == 200
    L = cs[0].get("/api/play").json()["lobby"]
    url = f"/api/lobbies/{L['id']}/chat"
    # nobody can chat during the ready check
    assert cs[0].post(url, json={"channel": "all", "body": "hi"}).status_code == 409
    for c in cs:
        c.post(f"/api/lobbies/{L['id']}/accept")
    by_name = {f"Chatter{first + i}": c for i, c in enumerate(cs)}
    return cs, by_name, cs[0].get("/api/play").json()["lobby"], url


def say(c, url, body, channel="all"):
    T[0] += 3  # stay under the rate limit
    return c.post(url, json={"channel": channel, "body": body})


def test_all_and_team_chat():
    cs, by_name, L, url = start_match(0)
    assert L["status"] == "drafting"
    caps = {p["team"]: by_name[p["name"]] for p in L["players"] if p["is_captain"]}
    benched = next(by_name[p["name"]] for p in L["players"] if not p["team"])

    assert say(benched, url, "  glhf   all  ").status_code == 201
    assert say(caps["red"], url, "red only", "team").status_code == 201
    assert say(caps["blue"], url, "blue only", "team").status_code == 201
    # not on a team yet, so no team chat
    assert say(benched, url, "hello team", "team").status_code == 409
    v = benched.get(url).json()
    assert v["channels"] == ["all"] and v["can_send"] == ["all"]
    assert [m["body"] for m in v["messages"]] == ["glhf all"]  # whitespace collapsed

    red = caps["red"].get(url).json()
    assert red["channels"] == ["all", "red"] and [m["body"] for m in red["messages"]] == ["glhf all", "red only"]
    assert red["messages"][1]["team"] == "red" and red["messages"][1]["mine"]

    # outsiders can't read it during the match; admins read everything
    assert player(99).get(url).status_code == 403
    a = admin().get(url).json()
    assert [m["body"] for m in a["messages"]] == ["glhf all", "red only", "blue only"]
    assert a["can_send"] == ["all"]

    # length cap and blank lines
    assert say(benched, url, "x" * (chat.CHAT_MAX_LEN + 1)).status_code == 400
    assert say(benched, url, "   ​ ").status_code == 400


def test_rate_limit_and_moderation():
    cs, by_name, L, url = start_match(10)
    c = cs[0]
    T[0] += 60
    for i in range(chat.CHAT_BURST):
        assert c.post(url, json={"channel": "all", "body": f"spam {i}"}).status_code == 201
    assert c.post(url, json={"channel": "all", "body": "one more"}).status_code == 429
    T[0] += chat.CHAT_WINDOW + 1
    assert c.post(url, json={"channel": "all", "body": "ok again"}).status_code == 201

    ad = admin()
    first = ad.get(url).json()["messages"][0]
    assert ad.post(f"/api/chat/{first['id']}/hide", json={"hidden": True}).status_code == 200
    assert c.post(f"/api/chat/{first['id']}/hide", json={"hidden": True}).status_code == 403
    assert "spam 0" not in [m["body"] for m in c.get(url).json()["messages"]]
    assert next(m for m in ad.get(url).json()["messages"] if m["id"] == first["id"])["hidden"] is True

    name = c.get("/api/me").json()["player"]["name"]
    assert ad.post("/api/chat/mute", json={"name": name, "minutes": 5, "reason": "spam"}).status_code == 200
    r = say(c, url, "muted?")
    assert r.status_code == 403 and "spam" in r.json()["detail"]
    assert c.get(url).json()["can_send"] == []
    assert any(name in m["names"] for m in ad.get("/api/chat/mutes").json())
    T[0] += 301
    assert say(c, url, "back").status_code == 201

    # an admin who isn't playing can post in All chat, marked as an admin
    assert say(ad, url, "behave please").status_code == 201
    assert c.get(url).json()["messages"][-1]["admin"] is True
    assert any(x["lobby_id"] == L["id"] for x in ad.get("/api/chat/recent").json())


def test_log_after_the_match():
    cs, by_name, L, url = start_match(20)
    caps = {p["team"]: by_name[p["name"]] for p in L["players"] if p["is_captain"]}
    assert say(cs[1], url, "gg").status_code == 201
    assert say(caps["red"], url, "secret plan", "team").status_code == 201
    with SessionLocal() as db:
        lobby = db.get(Lobby, L["id"])
        lobby.status, lobby.deadline = "finished", T[0]
        db.commit()
    assert say(cs[1], url, "post game gg").status_code == 201  # still open right after
    outsider = player(98).get(url).json()
    assert [m["body"] for m in outsider["messages"]] == ["gg", "post game gg"]  # no team chat
    assert outsider["can_send"] == []
    T[0] += chat.CHAT_AFTER_SECONDS + 1
    assert say(cs[1], url, "too late").status_code == 409
    v = caps["red"].get(url).json()
    assert not v["open"] and "secret plan" in [m["body"] for m in v["messages"]]
