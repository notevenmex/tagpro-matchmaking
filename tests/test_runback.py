from conftest import client
from sqlalchemy import delete, select
from test_reporting import played_lobby, report

from app import play, runback
from app.db import Lobby, LobbyPlayer, Player, QueueEntry, SessionLocal


def finished_lobby():
    """A lobby whose score both captains just confirmed, so its runback vote is open."""
    lid, ids = played_lobby()
    assert report(client(ids["red_cap"]), lid, 3, 1).json()["result"] == "waiting"
    assert report(client(ids["blue_cap"]), lid, 3, 1).json()["result"] == "verified"
    with SessionLocal() as db:
        dids = list(db.scalars(select(Player.discord_id).join(LobbyPlayer, LobbyPlayer.player_id == Player.id)
                               .where(LobbyPlayer.lobby_id == lid).order_by(LobbyPlayer.joined_at)))
    return lid, [client(d) for d in dids]


def lineup(lobby_id):
    with SessionLocal() as db:
        return sorted((p.tagpro_name, lp.team, lp.is_captain) for lp, p in
                      play._lobby_players(db, db.get(Lobby, lobby_id)))


def test_eight_yes_votes_start_the_same_match_again():
    lid, players = finished_lobby()
    rb = players[0].get("/api/play").json()["lobby"]["runback"]
    assert rb["status"] == "open" and rb["can_vote"] and rb["yes"] == 0 and rb["size"] == 8 and rb["deadline"]

    for c in players[:7]:
        assert c.post(f"/api/lobbies/{lid}/runback", json={"yes": True}).json()["status"] == "open"
    assert players[0].post(f"/api/lobbies/{lid}/runback", json={"yes": True}).status_code == 409  # one vote each
    assert players[0].get(f"/api/lobbies/{lid}").json()["runback"]["yes"] == 7
    res = players[7].post(f"/api/lobbies/{lid}/runback", json={"yes": True}).json()
    assert res["status"] == "passed"

    new = res["new_lobby_id"]
    with SessionLocal() as db:
        old, rematch = db.get(Lobby, lid), db.get(Lobby, new)
        assert rematch.status == "awaiting_group"  # straight to the group bot, no draft or veto
        assert (rematch.map_id, rematch.server) == (old.map_id, old.server)
    assert lineup(new) == lineup(lid)  # same teams and captains
    view = players[3].get("/api/play").json()["lobby"]
    assert view["id"] == new and view["runback_of"] == lid and view["status"] == "awaiting_group"


def test_one_no_cancels_and_frees_the_queue():
    lid, players = finished_lobby()
    assert "Salty Runback" in players[2].post("/api/queue").json()["detail"]
    players[0].post(f"/api/lobbies/{lid}/runback", json={"yes": True})
    assert players[1].post(f"/api/lobbies/{lid}/runback", json={"yes": False}).json()["status"] == "failed"
    rb = players[0].get(f"/api/lobbies/{lid}").json()["runback"]
    assert rb["status"] == "failed" and not rb["can_vote"] and rb["note"]
    assert players[2].post(f"/api/lobbies/{lid}/runback", json={"yes": True}).status_code == 409
    assert players[2].post("/api/queue").status_code == 200
    players[2].delete("/api/queue")


def test_vote_times_out(monkeypatch):
    lid, players = finished_lobby()
    start = play.clock()
    monkeypatch.setattr(play, "clock", lambda: start + runback.RUNBACK_SECONDS + 1)
    play._tick_once()
    rb = players[0].get(f"/api/lobbies/{lid}").json()["runback"]
    assert rb["status"] == "failed" and "in time" in rb["note"]


def test_queued_voters_keep_their_place_but_are_not_popped():
    with SessionLocal() as db:
        db.execute(delete(QueueEntry))
        db.commit()
    lid, players = finished_lobby()
    other_lid, others = played_lobby()  # 8 more players to fill the queue
    with SessionLocal() as db:
        held = db.scalar(select(LobbyPlayer.player_id).where(LobbyPlayer.lobby_id == lid))
        fill = list(db.scalars(select(LobbyPlayer.player_id).where(LobbyPlayer.lobby_id == other_lid)))[:7]
        db.get(Lobby, other_lid).status = "finished"
        for i, pid in enumerate([held] + fill):
            db.add(QueueEntry(player_id=pid, joined_at=i))
        db.flush()
        play.try_pop(db)
        assert len(db.scalars(select(QueueEntry)).all()) == 8  # the held player kept the pop from happening
        db.commit()
    players[5].post(f"/api/lobbies/{lid}/runback", json={"yes": False})
    with SessionLocal() as db:
        assert not db.scalars(select(QueueEntry)).all()  # the vote ended, so the queue popped
        db.execute(delete(LobbyPlayer).where(LobbyPlayer.lobby_id.in_(
            select(Lobby.id).where(Lobby.status == "ready_check"))))
        db.execute(delete(Lobby).where(Lobby.status == "ready_check"))
        db.commit()


def test_no_vote_after_an_admin_settles_it():
    lid, ids = played_lobby()
    admin = client("admin-1", "Admin")
    assert admin.post(f"/api/lobbies/{lid}/resolve", json={"red_score": 1, "blue_score": 3}).status_code == 200
    assert client(ids["red"]).get(f"/api/lobbies/{lid}").json()["runback"] is None


def test_no_runback_when_someone_is_already_in_another_match():
    lid, ids = played_lobby()
    other_lid, _ = played_lobby()
    with SessionLocal() as db:  # a non-captain queued during the game and is now drafting elsewhere
        pid = db.scalar(select(Player.id).where(Player.discord_id == ids["red"]))
        db.add(LobbyPlayer(lobby_id=other_lid, player_id=pid, joined_at=0, elo=1000))
        db.get(Lobby, other_lid).status = "drafting"
        db.commit()
    report(client(ids["red_cap"]), lid, 3, 1)
    report(client(ids["blue_cap"]), lid, 3, 1)
    rb = client(ids["blue"]).get(f"/api/lobbies/{lid}").json()["runback"]
    assert rb["status"] == "failed" and "another match" in rb["note"]
    assert client(ids["red_cap"]).post("/api/queue").status_code == 200
    client(ids["red_cap"]).delete("/api/queue")
    with SessionLocal() as db:
        db.get(Lobby, other_lid).status = "cancelled"
        db.commit()


def test_only_players_vote():
    lid, _ = finished_lobby()
    assert client("rb-outsider", "Outsider").post(f"/api/lobbies/{lid}/runback", json={"yes": True}).status_code in (403, 401)


def test_post_match_page_and_recent_matches():
    lid, players = finished_lobby()
    with SessionLocal() as db:
        lobby = db.get(Lobby, lid)
        mid = lobby.match_id
        names = [p.tagpro_name for _, p in play._lobby_players(db, lobby)]
    page = players[0].get(f"/api/matches/{mid}").json()
    assert page["lobby_id"] == lid and (page["red_score"], page["blue_score"], page["winner"]) == (3, 1, "red")
    assert page["verified_by"] == "captains" and page["map"] and page["server"] == "US East"
    assert len(page["teams"]["red"]) == 4 and page["teams"]["red"][0]["is_captain"]
    assert page["runback"]["status"] == "open" and page["me"]["team"] in ("red", "blue")
    assert client().get(f"/api/matches/{mid}").json()["me"] is None  # public, archivable page
    assert client().get(f"/match/{mid}", follow_redirects=False).headers["location"] == f"/#match/{mid}"
    assert client().get("/api/matches/999999").status_code == 404
    assert players[0].get("/api/play").json()["lobby"]["match_id"] == mid

    # the profile lists the match and links to it; the player can hide the list or change its length
    prof = client().get(f"/api/players/{names[0]}").json()
    assert prof["recent"][0]["match_id"] == mid and prof["recent"][0]["red_score"] == 3 and not prof["own"]
    assert players[0].patch("/api/me/match-history", json={"show": False, "count": 5}).json() == {"show": False, "count": 5}
    hidden = client().get(f"/api/players/{names[0]}").json()
    assert hidden["recent_hidden"] and hidden["recent"] == []
    own = players[0].get(f"/api/players/{names[0]}").json()
    assert own["own"] and own["recent"] and own["recent_count"] == 5
    assert players[0].patch("/api/me/match-history", json={"count": 7}).status_code == 422
    players[0].patch("/api/me/match-history", json={"show": True})
    assert client().get(f"/api/players/{names[0]}").json()["recent"]
