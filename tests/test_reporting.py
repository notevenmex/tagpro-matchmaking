from conftest import client

from app import play
from app.db import Lobby, LobbyPlayer, Player, Rating, SessionLocal, Season
from sqlalchemy import select

_n = 0


def played_lobby():
    """A lobby whose game has started (group link posted), with fresh players. Returns
    (lobby id, {"red_cap", "blue_cap", "red", "blue"} -> Discord id of one player each)."""
    global _n
    _n += 1
    with SessionLocal() as db:
        season = db.scalar(select(Season).where(Season.is_active.is_(True)))
        lobby = Lobby(season_id=season.id, status="in_progress", created_at=play.clock(), map_pool=[],
                      map_id=1, server="US East", group_link="https://tagpro.koalabeast.com/groups/abc")
        db.add(lobby)
        db.flush()
        ids = {}
        for i in range(8):
            team = "red" if i % 2 == 0 else "blue"
            did = f"rp{_n}-{i}"
            p = Player(discord_id=did, tagpro_name=f"Rep{_n}x{i}", position="offense" if i < 4 else "defense", tags=[])
            db.add(p)
            db.flush()
            db.add(LobbyPlayer(lobby_id=lobby.id, player_id=p.id, joined_at=i, elo=1000, accepted=True,
                               team=team, is_captain=i < 2, pick_no=None if i < 2 else i - 1))
            ids.setdefault(team + "_cap" if i < 2 else team, did)
        db.commit()
        return lobby.id, ids


def report(c, lobby_id, red, blue):
    return c.post(f"/api/lobbies/{lobby_id}/report", json={"red_score": red, "blue_score": blue})


def games(name):
    with SessionLocal() as db:
        p = db.scalar(select(Player).where(Player.tagpro_name == name))
        r = db.scalar(select(Rating).where(Rating.player_id == p.id))
        return r.games if r else 0


def test_matching_reports_verify_and_free_the_captains():
    lid, ids = played_lobby()
    red_cap, blue_cap = client(ids["red_cap"]), client(ids["blue_cap"])
    red, blue = client(ids["red"]), client(ids["blue"])

    # captains can't queue until the score is in and verified; everyone else can
    assert "Report the final score" in red_cap.post("/api/queue").json()["detail"]
    assert red.post("/api/queue").status_code == 200
    assert red.delete("/api/queue").status_code == 200

    # only captains report, and ties or bad numbers are refused
    assert report(blue, lid, 3, 1).status_code == 403
    assert report(red_cap, lid, 2, 2).status_code == 422
    assert report(red_cap, lid, -1, 2).status_code == 422

    assert report(red_cap, lid, 3, 1).json()["result"] == "waiting"
    assert "Waiting for the other captain" in red_cap.post("/api/queue").json()["detail"]
    assert "Report the final score" in blue_cap.post("/api/queue").json()["detail"]
    score = red_cap.get(f"/api/lobbies/{lid}/score").json()
    assert score["reported"] == ["red"] and score["reports"]["red"] == {"red_score": 3, "blue_score": 1}
    assert blue.get(f"/api/lobbies/{lid}/score").json()["reports"] is None  # players don't see the reports

    assert report(blue_cap, lid, 3, 1).json()["result"] == "verified"
    score = blue.get(f"/api/lobbies/{lid}/score").json()
    assert score["final"] == {"red_score": 3, "blue_score": 1, "winner": "red", "verified_by": "captains"}
    assert games(f"Rep{_n}x0") == 1 and games(f"Rep{_n}x7") == 1  # Elo was updated for all 8

    assert report(red_cap, lid, 3, 1).status_code == 409  # already finished
    assert red_cap.post("/api/queue").status_code == 200
    assert blue_cap.post("/api/queue").status_code == 200
    red_cap.delete("/api/queue"), blue_cap.delete("/api/queue")


def test_mismatch_goes_to_admin_dispute_queue():
    lid, ids = played_lobby()
    red_cap, blue_cap = client(ids["red_cap"]), client(ids["blue_cap"])
    admin = client("admin-1", "Admin")

    assert report(red_cap, lid, 3, 1).json()["result"] == "waiting"
    assert report(blue_cap, lid, 1, 3).json()["result"] == "disputed"
    assert "admin settles it" in red_cap.post("/api/queue").json()["detail"]
    assert "admin settles it" in blue_cap.post("/api/queue").json()["detail"]

    assert red_cap.get("/api/disputes").status_code == 403
    d = next(x for x in admin.get("/api/disputes").json() if x["id"] == lid)
    assert d["reports"]["red"]["red_score"] == 3 and d["reports"]["blue"]["red_score"] == 1
    assert len(d["teams"]["red"]) == 4 and d["map"]

    assert red_cap.post(f"/api/lobbies/{lid}/resolve", json={"red_score": 1, "blue_score": 3}).status_code == 403
    assert admin.post(f"/api/lobbies/{lid}/resolve", json={"red_score": 1, "blue_score": 3}).status_code == 200
    assert all(x["id"] != lid for x in admin.get("/api/disputes").json())
    final = red_cap.get(f"/api/lobbies/{lid}/score").json()["final"]
    assert final == {"red_score": 1, "blue_score": 3, "winner": "blue", "verified_by": "admin"}
    assert red_cap.post("/api/queue").status_code == 200
    red_cap.delete("/api/queue")


def test_captain_can_fix_a_typo_during_a_dispute():
    lid, ids = played_lobby()
    red_cap, blue_cap = client(ids["red_cap"]), client(ids["blue_cap"])
    assert report(red_cap, lid, 4, 2).json()["result"] == "waiting"
    assert report(blue_cap, lid, 4, 3).json()["result"] == "disputed"
    assert report(blue_cap, lid, 4, 2).json()["result"] == "verified"
    assert blue_cap.post("/api/queue").status_code == 200
    blue_cap.delete("/api/queue")


def test_admin_can_settle_a_match_one_captain_never_reported():
    lid, ids = played_lobby()
    red_cap = client(ids["red_cap"])
    admin = client("admin-1", "Admin")
    assert admin.post(f"/api/lobbies/{lid}/resolve", json={"red_score": 0, "blue_score": 2}).status_code == 200
    assert red_cap.post("/api/queue").status_code == 200
    red_cap.delete("/api/queue")


def test_cannot_report_before_the_game_starts():
    lid, ids = played_lobby()
    with SessionLocal() as db:
        db.get(Lobby, lid).status = "awaiting_group"
        db.commit()
    assert report(client(ids["red_cap"]), lid, 3, 1).status_code == 409
