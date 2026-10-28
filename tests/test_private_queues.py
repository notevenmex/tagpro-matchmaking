from sqlalchemy import delete

from app import play
from app.db import PrivateQueueEntry, QueueEntry, SessionLocal
from test_play import T, advance, lobby, player
from test_restrictions import admin


def clear_queues():
    with SessionLocal() as db:
        db.execute(delete(QueueEntry))
        db.execute(delete(PrivateQueueEntry))
        db.commit()


def make_queue(name, *members):
    a = admin()
    q = a.post("/api/admin/queues", json={"name": name})
    assert q.status_code == 201, q.text
    for m in members:
        assert a.post(f"/api/admin/queues/{q.json()['id']}/members", json={"name": m}).status_code == 201
    return q.json()["id"]


def name(c):
    return c.get("/api/me").json()["player"]["name"]


def mine(c, qid):
    return next((q for q in c.get("/api/play").json()["queues"] if q["id"] == qid), None)


def test_admins_manage_queues_and_only_members_see_and_join():
    clear_queues()
    me, other = player(400), player(401)
    assert me.post("/api/admin/queues", json={"name": "Nope"}).status_code == 403
    qid = make_queue("S Queue", name(me))
    assert admin().post("/api/admin/queues", json={"name": "s queue"}).status_code == 409  # names are unique

    assert mine(me, qid)["member"] and mine(other, qid) is None  # hidden from non-members
    r = other.post(f"/api/queues/{qid}")
    assert r.status_code == 403 and "S Queue" in r.json()["detail"]
    assert me.post(f"/api/queues/{qid}").status_code == 200
    assert mine(me, qid)["in_queue"] and mine(me, qid)["queue"] == [name(me)]

    a = admin()
    q = a.patch(f"/api/admin/queues/{qid}", json={"name": "S Queue+"}).json()
    assert q["name"] == "S Queue+" and q["members"] == [name(me)]
    assert {"id": qid, "name": "S Queue+"} in me.get("/api/queues").json()  # listed for the leaderboard tabs
    a.delete(f"/api/admin/queues/{qid}/members/{name(me)}")  # removing a member takes them out of its queue
    assert mine(me, qid) is None
    assert a.post(f"/api/admin/queues/{qid}/members", json={"name": "Nobody Here"}).status_code == 404
    assert a.delete(f"/api/admin/queues/{qid}").status_code == 200
    assert all(x["id"] != qid for x in a.get("/api/admin/queues").json())
    assert me.post(f"/api/queues/{qid}").status_code == 404  # deleted queues are gone for players
    assert make_queue("S Queue+")  # and the name can be used again


def test_queue_in_both_and_a_pop_takes_you_out_of_the_other():
    clear_queues()
    me = player(410)
    qid = make_queue("Pop Queue", name(me))
    assert me.post("/api/queue").status_code == 200
    assert me.post(f"/api/queues/{qid}").status_code == 200
    st = me.get("/api/play").json()
    assert st["in_queue"] and mine(me, qid)["in_queue"]  # waiting in both at once

    me.post(f"/api/dev/fill-queue?queue_id={qid}")  # bots fill the private queue, so it pops
    L = lobby(me)
    assert L["status"] == "ready_check" and L["queue"] == "Pop Queue" and len(L["players"]) == 8
    st = me.get("/api/play").json()
    assert not st["in_queue"] and not mine(me, qid)["in_queue"]  # out of every queue
    assert me.post("/api/queue").status_code == 409  # in a match now

    me.post(f"/api/lobbies/{L['id']}/decline")  # the others go back to the private queue, not the general one
    q = mine(admin(), qid)
    assert len(q["queue"]) == 7 and name(me) not in q["queue"]
    assert me.get("/api/play").json()["queue"] == []
    advance(me.get("/api/play").json()["banned_until"] - T[0] + 1)
    clear_queues()


def test_rules_apply_to_private_queues():
    clear_queues()
    c = player(420)
    main = name(c)
    assert c.post("/api/register", json={"tagpro_name": "AltOf420", "position": "offense"}).status_code == 201
    qid = make_queue("Rules Queue", main, "AltOf420")
    assert c.post(f"/api/queues/{qid}").status_code == 200  # the alt queues privately
    c.post("/api/me/account", json={"name": main})
    r = c.post("/api/queue")
    assert r.status_code == 409 and "AltOf420" in r.json()["detail"]  # one account at a time, across queues
    assert c.post(f"/api/queues/{qid}").status_code == 409

    a = admin()
    a.post("/api/admin/bans", json={"name": main, "minutes": 30, "reason": "test"})
    assert mine(a, qid)["queue"] == []  # a ban clears every queue, on every linked account
    assert c.post(f"/api/queues/{qid}").status_code == 403
    a.delete(f"/api/admin/bans/{main}")

    assert c.post(f"/api/queues/{qid}").status_code == 200
    assert c.delete("/api/queues").status_code == 200  # leave everything
    assert not mine(c, qid)["in_queue"]
    c.post(f"/api/queues/{qid}")
    advance(46 * 60)  # idle timeout applies too
    assert not mine(c, qid)["in_queue"]
    clear_queues()


def test_each_private_queue_has_its_own_elo_and_leaderboard():
    from sqlalchemy import select
    from app.db import Player, Rating, Season
    from app.rating import PLACEMENT_GAMES, find_rating, record_match
    qid = make_queue("Elo Queue")
    with SessionLocal() as db:
        s = db.scalar(select(Season).where(Season.is_active.is_(True)))
        ps = [Player(tagpro_name=f"Elo{i}", position="offense", tags=[]) for i in range(8)]
        db.add_all(ps)
        db.flush()
        lineup = [(p, "red" if i < 4 else "blue", "offense") for i, p in enumerate(ps)]
        for _ in range(PLACEMENT_GAMES):
            record_match(db, s, winner="red", reported_by="test", lineup=lineup, queue_id=qid)
        r = find_rating(db, ps[0].id, s.id, qid)
        assert r.games == PLACEMENT_GAMES and r.elo > 1000
        assert db.scalar(select(Rating).where(Rating.player_id == ps[0].id)) is None  # general Elo untouched
        _, ch = record_match(db, s, winner="red", reported_by="test", lineup=lineup)  # a general match
        assert ch[0]["before"] == 1000 and ch[0]["multiplier"] == 1  # no streak carried over from the queue
        db.commit()
        names = [p.tagpro_name for p in ps]
    c = admin()
    board = c.get(f"/api/leaderboard?queue_id={qid}").json()
    assert {b["name"] for b in board} == set(names) and {b["name"] for b in board[:4]} == set(names[:4])  # red won
    assert not any(b["name"] in names for b in c.get("/api/leaderboard").json())  # 1 general game: still placing
    prof = c.get("/api/players/Elo0").json()
    q = next(x for x in prof["queues"] if x["id"] == qid)
    assert q["name"] == "Elo Queue" and len(q["history"]) == PLACEMENT_GAMES
    assert q["rating"]["elo"] == q["history"][-1]["elo"] == next(b["elo"] for b in board if b["name"] == "Elo0")
    assert prof["recent"][1]["queue"] == "Elo Queue" and prof["recent"][1]["change"] > 0
    assert prof["recent"][0]["queue"] is None and prof["recent"][0]["change"] is None  # general placements
