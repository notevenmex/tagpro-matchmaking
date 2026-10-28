from conftest import client


def test_public_pages():
    c = client()
    assert c.get("/").status_code == 200
    assert c.get("/api/season").json()["number"] == 4
    board = c.get("/api/leaderboard").json()
    assert board[0]["name"] == "Warriors" and board[0]["elo"] == 2023
    assert c.get("/api/rating-rules").json()["start_elo"] == 1000
    maps = c.get("/api/maps").json()
    assert len(maps) == 12 and all(m["chance"] == 58.33 for m in maps)  # 7 of 12 drawn, votes ignored


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
    assert ch["NewGuy"]["before"] == 1000 and ch["NewGuy"]["change"] > 25  # underdog win, in placements
    assert ch["Warriors"]["change"] < 0
    assert ch["Spud"]["change"] < ch["NewGuy"]["change"]  # Spud is placed, so he moves the normal amount
    detail = u.get("/api/players/NewGuy").json()
    assert detail["recent"][0]["won"] is True and detail["recent"][0]["change"] is None  # hidden in placements
    assert detail["rating"] == {"elo": None, "games": 1, "wins": 1, "placements_left": 9}
    assert u.get("/api/players/Spud").json()["recent"][0]["change"] == ch["Spud"]["change"]

    bad = dict(body, players=body["players"] + [{"tagpro_name": "Nobody", "team": "red", "position": "offense"}])
    assert "Nobody" in a.post("/api/matches", json=bad).json()["detail"]
