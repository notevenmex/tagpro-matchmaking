from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import play, restrictions
from app.db import Player, QueueEntry, Rating, SessionLocal
from app.main import app
from test_play import T, advance, lobby, player  # shares test_play's fake clock

H = 3600


def clear_queue():
    with SessionLocal() as db:
        db.execute(delete(QueueEntry))
        db.commit()


def admin():
    a = TestClient(app)
    a.get("/auth/dev-login", params={"discord_id": "admin-1", "name": "Admin"}, follow_redirects=False)
    if a.get("/api/me").json()["player"] is None:
        a.post("/api/register", json={"tagpro_name": "AdminGuy", "position": "offense"})
    return a


def test_ready_check_cooldowns_escalate_and_reset():
    clear_queue()
    me = player(200)
    cooldowns = []
    for _ in range(3):
        assert me.post("/api/queue").status_code == 200
        me.post("/api/dev/fill-queue")
        L = lobby(me)
        assert L["status"] == "ready_check"
        me.post(f"/api/lobbies/{L['id']}/decline")
        st = me.get("/api/play").json()
        cooldowns.append(round((st["banned_until"] - T[0]) / 60))
        r = me.post("/api/queue")
        assert r.status_code == 403 and r.json()["detail"] == st["ban_reason"]
        advance(st["banned_until"] - T[0] + 1)
    assert cooldowns == restrictions.READY_CHECK_COOLDOWNS[:3]
    assert "third time" in st["ban_reason"]

    advance(restrictions.STRIKE_RESET_HOURS * H)  # a clean day resets the chain
    me.post("/api/queue")
    me.post("/api/dev/fill-queue")
    me.post(f"/api/lobbies/{lobby(me)['id']}/decline")
    st = me.get("/api/play").json()
    assert round((st["banned_until"] - T[0]) / 60) == restrictions.READY_CHECK_COOLDOWNS[0]
    assert "first time" in st["ban_reason"]
    advance(st["banned_until"] - T[0] + 1)
    clear_queue()


def test_admin_bans():
    clear_queue()
    me, a = player(210), admin()
    name = me.get("/api/me").json()["player"]["name"]
    me.post("/api/queue")
    assert me.post("/api/admin/bans", json={"name": name}).status_code == 403  # not an admin

    assert a.post("/api/admin/bans", json={"name": name, "minutes": 90, "reason": "toxic in chat"}).status_code == 201
    st = me.get("/api/play").json()
    assert not st["in_queue"]  # a ban takes you out of the queue
    detail = me.post("/api/queue").json()["detail"]
    assert detail.startswith("An admin banned you from queueing: toxic in chat.") and detail.endswith("You can queue again in 1 h 30 min.")

    a.post("/api/admin/bans", json={"name": name, "reason": "cheating"})  # permanent replaces it
    assert "permanent" in me.post("/api/queue").json()["detail"]
    bans = {b["name"]: b for b in a.get("/api/admin/bans").json()}
    assert bans[name]["permanent"] and bans[name]["reason"] == "cheating"

    assert a.delete(f"/api/admin/bans/{name}").status_code == 200
    assert me.post("/api/queue").status_code == 200
    clear_queue()


def test_idle_queue_expires_but_accepting_counts_as_activity():
    clear_queue()
    me = player(220)
    me.post("/api/queue")
    advance(restrictions.QUEUE_IDLE_MINUTES * 60 - 60)
    assert me.get("/api/play").json()["in_queue"]
    advance(120)
    assert not me.get("/api/play").json()["in_queue"]
    clear_queue()


def _to_awaiting_group(cs):
    for c in cs:
        assert c.post("/api/queue").status_code == 200
    L = lobby(cs[0])
    for c in cs:
        c.post(f"/api/lobbies/{L['id']}/accept")
    for _ in range(20):  # timeouts finish the draft, veto and server vote
        if lobby(cs[0])["status"] == "awaiting_group":
            break
        advance(max(play.PICK_SECONDS, play.BAN_SECONDS, play.VOTE_SECONDS) + 1)
    L = lobby(cs[0])
    assert L["status"] == "awaiting_group"
    return L


def test_leaver_needs_both_captains_and_voids_the_match():
    clear_queue()
    cs = [player(230 + i) for i in range(8)]
    names = [c.get("/api/me").json()["player"]["name"] for c in cs]
    by_name = dict(zip(names, cs))
    L = _to_awaiting_group(cs)
    caps = {p["team"]: by_name[p["name"]] for p in L["players"] if p["is_captain"]}
    leaver = next(p["name"] for p in L["players"] if not p["is_captain"])
    other = next(p["name"] for p in L["players"] if not p["is_captain"] and p["name"] != leaver)

    assert by_name[other].post(f"/api/lobbies/{L['id']}/leaver", json={"name": leaver}).status_code == 403
    r = caps["red"].post(f"/api/lobbies/{L['id']}/leaver", json={"name": leaver})
    assert r.status_code == 200 and r.json()["confirmed"] is False
    assert by_name[leaver].post("/api/queue").status_code == 409  # still in the match, not banned yet
    r = caps["blue"].post(f"/api/lobbies/{L['id']}/leaver", json={"name": leaver})
    assert r.json()["confirmed"] is True

    L = lobby(cs[0])
    assert L["status"] == "cancelled" and "Voided" in L["note"]
    st = by_name[leaver].get("/api/play").json()
    assert round((st["banned_until"] - T[0]) / 60) == restrictions.LEAVER_COOLDOWNS[0]
    assert "leaving match" in st["ban_reason"]
    with SessionLocal() as db:
        pid = db.scalar(select(Player.id).where(Player.tagpro_name == leaver))
        elos = db.scalars(select(Rating.elo).where(Rating.player_id == pid)).all()
    assert elos and all(e == restrictions.START_ELO - restrictions.LEAVER_ELO for e in elos)
    # flagging again doesn't stack a second penalty
    caps["red"].post(f"/api/lobbies/{L['id']}/leaver", json={"name": leaver})
    assert by_name[other].post("/api/queue").status_code == 200  # everyone else can queue right away
    clear_queue()


def test_alt_accounts_queue_one_at_a_time_and_share_bans():
    clear_queue()
    c = player(300)  # registers Queuer300 on Discord q-300
    main = c.get("/api/me").json()["player"]["name"]
    assert c.post("/api/register", json={"tagpro_name": "AltOf300", "position": "defense"}).status_code == 201
    me = c.get("/api/me").json()
    assert me["player"]["name"] == "AltOf300" and me["accounts"] == [main, "AltOf300"]

    assert c.post("/api/queue").status_code == 200  # the alt queues
    assert c.post("/api/me/account", json={"name": main}).json()["name"] == main
    r = c.post("/api/queue")
    assert r.status_code == 409 and "AltOf300" in r.json()["detail"]  # only one account at a time
    c.post("/api/me/account", json={"name": "AltOf300"})
    c.delete("/api/queue")
    c.post("/api/me/account", json={"name": main})
    assert c.post("/api/queue").status_code == 200

    # a ban on one account covers the others
    admin().post("/api/admin/bans", json={"name": "AltOf300", "minutes": 30, "reason": "smurfing"})
    c.post("/api/me/account", json={"name": "AltOf300"})
    c.delete("/api/queue")
    c.post("/api/me/account", json={"name": main})
    c.delete("/api/queue")
    r = c.post("/api/queue")
    assert r.status_code == 403 and "smurfing" in r.json()["detail"]
    assert c.get("/api/play").json()["banned_until"]
    assert c.post("/api/me/account", json={"name": "Warriors"}).status_code == 404  # not yours

    for i in range(2, 4):  # the cap is MAX_ACCOUNTS
        c.post("/api/register", json={"tagpro_name": f"Alt{i}Of300", "position": "offense"})
    assert len(c.get("/api/me").json()["accounts"]) == 3
    admin().delete("/api/admin/bans/AltOf300")
    clear_queue()


def test_players_choose_and_change_position():
    c = player(310)
    assert c.get("/api/me").json()["player"]["position"] == "offense"
    assert c.patch("/api/me", json={"position": "flex"}).json()["position"] == "flex"
    assert c.patch("/api/me", json={"position": "goalie"}).status_code == 422
    assert c.patch("/api/me", json={"server": "Chord"}).json() == {**c.get("/api/me").json()["player"], "server": "Chord"}
    assert c.get("/api/me").json()["player"]["position"] == "flex"  # untouched by the server change
    assert c.post("/api/register", json={"tagpro_name": "FlexAlt310", "position": "flex"}).status_code == 201
    assert TestClient(app).patch("/api/me", json={"position": "defense"}).status_code == 401
