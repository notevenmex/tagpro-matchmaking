import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["DEV_LOGIN"] = "1"
os.environ["ADMIN_DISCORD_IDS"] = "admin-1"

from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.main import app  # noqa: E402

seed.run()


def client(discord_id=None, name="Tester"):
    c = TestClient(app)
    if discord_id:
        c.get("/auth/dev-login", params={"discord_id": discord_id, "name": name}, follow_redirects=False)
    return c


def test_public_pages():
    c = client()
    assert c.get("/").status_code == 200
    assert c.get("/api/season").json()["number"] == 4
    board = c.get("/api/leaderboard").json()
    assert board[0]["name"] == "Warriors" and board[0]["rating"] == 43
    maps = c.get("/api/maps").json()
    assert len(maps) == 10 and abs(sum(m["chance"] for m in maps) - 100) < 0.1


def test_register_vote_and_report():
    u = client("user-1", "Miguel")
    assert u.put("/api/maps/1/vote", json={"score": 5}).status_code == 401  # not registered yet
    r = u.post("/api/register", json={"tagpro_name": "NewGuy", "position": "defense"})
    assert r.status_code == 201
    assert u.post("/api/register", json={"tagpro_name": "Neon", "position": "offense"}).status_code == 409
    assert u.put("/api/maps/1/vote", json={"score": 11}).status_code == 422
    assert u.put("/api/maps/1/vote", json={"score": 10}).status_code == 200
    assert next(m for m in u.get("/api/maps").json() if m["id"] == 1)["my_vote"] == 10

    # only admins can report
    assert u.post("/api/matches", json={"winner": "red", "players": []}).status_code == 403
    a = client("admin-1", "Admin")
    body = {"winner": "red", "map_id": 1, "players": [
        {"tagpro_name": "NewGuy", "team": "red", "position": "defense"},
        {"tagpro_name": "spud", "team": "red", "position": "offense"},
        {"tagpro_name": "Warriors", "team": "blue", "position": "offense"},
        {"tagpro_name": "Neon", "team": "blue", "position": "defense"},
    ]}
    res = a.post("/api/matches", json=body)
    assert res.status_code == 201, res.text
    ch = {c["name"]: c for c in res.json()["changes"]}
    assert ch["NewGuy"]["after"] > ch["NewGuy"]["before"]
    assert ch["Warriors"]["after"] < ch["Warriors"]["before"]
    detail = u.get("/api/players/NewGuy").json()
    assert detail["recent"][0]["won"] is True and detail["ratings"][0]["games"] == 1

    bad = dict(body, players=body["players"] + [{"tagpro_name": "Nobody", "team": "red", "position": "offense"}])
    assert "Nobody" in a.post("/api/matches", json=bad).json()["detail"]
