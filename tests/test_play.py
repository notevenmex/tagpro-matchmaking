import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("ADMIN_DISCORD_IDS", "admin-1")

from fastapi.testclient import TestClient  # noqa: E402

from app import play, restrictions, seed  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

seed.run()
T = [1_000_000.0]
play.clock = lambda: T[0]


def advance(seconds):
    T[0] += seconds
    with SessionLocal() as db:
        play.tick(db)
        db.commit()


def player(i, pos="offense"):
    c = TestClient(app)
    c.get("/auth/dev-login", params={"discord_id": f"q-{i}", "name": f"Q{i}"}, follow_redirects=False)
    if c.get("/api/me").json()["player"] is None:
        assert c.post("/api/register", json={"tagpro_name": f"Queuer{i}", "position": pos}).status_code == 201
    return c


def lobby(c):
    return c.get("/api/play").json()["lobby"]


def test_full_match_flow():
    cs = [player(i, "offense" if i % 2 else "defense") for i in range(8)]
    for c in cs[:7]:
        assert c.post("/api/queue").status_code == 200
    assert len(cs[0].get("/api/play").json()["queue"]) == 7
    cs[7].post("/api/queue")
    L = lobby(cs[0])
    assert L["status"] == "ready_check" and len(L["players"]) == 8 and L["step_seconds"] == play.READY_SECONDS
    assert cs[0].get("/api/play").json()["queue"] == []

    for c in cs:
        assert c.post(f"/api/lobbies/{L['id']}/accept").status_code == 200
    L = lobby(cs[0])
    assert L["status"] == "drafting" and L["turn"] == "red"
    caps = {p["team"]: p["name"] for p in L["players"] if p["is_captain"]}
    # the home page's ongoing matches list sees it, with the captains on their teams and no map yet
    live = next(x for x in cs[0].get("/api/play").json()["live"] if x["id"] == L["id"])
    assert live["map"] is None and live["group_link"] is None and live["avg_elo"] == round(sum(p["elo"] for p in L["players"]) / 8)
    assert live["teams"]["red"]["players"] == [caps["red"]] and live["teams"]["blue"]["players"] == [caps["blue"]]
    by_name = {f"Queuer{i}": c for i, c in enumerate(cs)}
    red, blue = by_name[caps["red"]], by_name[caps["blue"]]
    free = [p["name"] for p in L["players"] if not p["team"]]
    assert blue.post(f"/api/lobbies/{L['id']}/pick", json={"name": free[0]}).status_code == 403  # not blue's turn
    for team in ["red", "blue", "blue", "red", "red"]:
        L = lobby(cs[0])
        assert L["turn"] == team
        free = [p["name"] for p in L["players"] if not p["team"]]
        cap = red if team == "red" else blue
        assert cap.post(f"/api/lobbies/{L['id']}/pick", json={"name": free[0]}).status_code == 200, team
    L = lobby(cs[0])  # last player was a forced pick
    assert L["status"] == "map_veto" and L["turn"] == "blue"
    assert sum(p["team"] == "red" for p in L["players"]) == 4
    assert len(L["map_pool"]) == play.MAP_POOL_SIZE and len({m["id"] for m in L["map_pool"]}) == play.MAP_POOL_SIZE

    while L["status"] == "map_veto":
        cap = red if L["turn"] == "red" else blue
        m = next(m for m in L["map_pool"] if not m["banned_by"])
        assert cap.post(f"/api/lobbies/{L['id']}/ban", json={"map_id": m["id"]}).status_code == 200
        L = lobby(cs[0])
    assert L["status"] == "server_vote" and L["map"]
    for c in cs:
        c.post(f"/api/lobbies/{L['id']}/server", json={"server": play.SERVERS[0]})
    L = lobby(cs[0])
    assert L["status"] == "awaiting_group" and L["server"] == play.SERVERS[0]
    assert blue.post(f"/api/lobbies/{L['id']}/group", json={"link": "https://tagpro.koalabeast.com/groups/abc"}).status_code == 403
    assert red.post(f"/api/lobbies/{L['id']}/group", json={"link": "https://evil.example/groups/abc"}).status_code == 400
    assert red.post(f"/api/lobbies/{L['id']}/group", json={"link": "https://tagpro.koalabeast.com/groups/abc"}).status_code == 200
    L = lobby(cs[3])
    assert L["status"] == "in_progress" and L["group_link"].endswith("/abc")


def test_ready_check_timeout_bans_and_requeues():
    cs = [player(10 + i) for i in range(8)]
    for c in cs:
        c.post("/api/queue")
    L = lobby(cs[0])
    assert L["status"] == "ready_check"
    for c in cs[:6]:
        c.post(f"/api/lobbies/{L['id']}/accept")
    advance(play.READY_SECONDS + 1)
    st = cs[0].get("/api/play").json()
    assert st["lobby"]["status"] == "cancelled" and st["in_queue"] and len(st["queue"]) == 6
    late = cs[7].get("/api/play").json()
    assert late["banned_until"] and not late["in_queue"]
    assert cs[7].post("/api/queue").status_code == 403
    advance(restrictions.READY_CHECK_COOLDOWNS[0] * 60 + 1)
    assert cs[7].post("/api/queue").status_code == 200
    for c in cs:
        c.delete("/api/queue")


def test_decline_and_timeouts_with_bots():
    me = player(30)
    me.post("/api/queue")
    assert me.post("/api/dev/fill-queue").status_code == 200
    L = lobby(me)
    assert L["status"] == "ready_check"
    assert me.post(f"/api/lobbies/{L['id']}/decline").status_code == 200
    assert lobby(me)["status"] == "cancelled" and me.get("/api/play").json()["banned_until"]
    advance(restrictions.READY_CHECK_COOLDOWNS[0] * 60 + 1)  # ban ends; the 7 bots are still queued

    me.post("/api/queue")
    L = lobby(me)
    assert L["status"] == "ready_check"
    me.post(f"/api/lobbies/{L['id']}/accept")
    advance(play.BOT_DELAY)  # bots accept
    assert lobby(me)["status"] == "drafting"
    for _ in range(12):  # timeouts and bot captains finish the draft and veto
        advance(play.BAN_SECONDS + 1)
    L = lobby(me)
    assert L["status"] == "awaiting_group" and L["map"] and L["server"]
    assert all(p["team"] for p in L["players"])
