from conftest import client


def test_titles_admin_and_display():
    assert client().post("/api/admin/titles", json={"season": 4, "title": "Champion"}).status_code == 401
    assert client("titles-player").post("/api/admin/titles", json={"season": 4, "title": "Champion"}).status_code == 403

    a = client("admin-1")
    assert a.post("/api/admin/titles", json={"season": 4, "title": "Champion", "player": "Nobody Here"}).status_code == 400
    tid = a.post("/api/admin/titles", json={"season": 4, "title": "Champion", "reward": "Gold role"}).json()["id"]
    t = next(x for x in client().get("/api/titles", params={"season": 4}).json() if x["id"] == tid)
    assert t["player"] is None and t["reward"] == "Gold role"

    # awarding it shows the title on the player's profile and the leaderboard; archive tags are hidden
    assert a.patch(f"/api/admin/titles/{tid}", json={"player": "Warriors"}).status_code == 200
    held = {"label": "S4 Champion", "reward": "Gold role"}
    assert held in client().get("/api/players/Warriors").json()["titles"]
    row = next(p for p in client().get("/api/leaderboard").json() if p["name"] == "Warriors")
    assert held in row["titles"] and "tags" not in row
    swerve = client().get("/api/players/swerve").json()
    assert swerve["titles"] == [] and "tags" not in swerve  # had OG / S3 tags in the 2017 archive

    assert a.patch(f"/api/admin/titles/{tid}", json={"player": ""}).status_code == 200
    assert client().get("/api/players/Warriors").json()["titles"] == []
    assert a.delete(f"/api/admin/titles/{tid}").status_code == 200
    assert all(x["id"] != tid for x in client().get("/api/titles").json())
